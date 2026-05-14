/*
 * Subnautica 2 - Interactive Leaflet Map
 *
 * Everything is rendered in code:
 *   - Base layer: client-side canvas hillshade tinted by biome polygons (from the
 *     extracted heightmap.json + organic biome polygons). No pre-baked PNG.
 *   - Optional depth contour overlay (marching-squares).
 *   - Biome polygons as a vector overlay with strokes + labels.
 *   - World wall polygon as the playable-area border.
 *   - All 12,350 placements as clustered markers.
 *
 * Coordinate system: L.CRS.Simple, latlng = (Y_world, X_world) in UE cm.
 */

(function() {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function fmtNum(n, digits = 0) {
    if (n == null || !isFinite(n)) return '-';
    return n.toLocaleString(undefined, {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function hexToRgb(hex) {
    const h = hex.replace('#', '');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }

  // Per-category circle-marker style on the canvas renderer.
  const CAT_STYLE = {
    resources: { radius: 2.8, weight: 0.6, fillOpacity: 0.95 },
    creatures: { radius: 4.0, weight: 0.7, fillOpacity: 0.95 },
    pois:      { radius: 5.0, weight: 1.0, fillOpacity: 0.95 },
    loot:      { radius: 4.0, weight: 0.8, fillOpacity: 0.95 },
    caves:     { radius: 6.5, weight: 1.5, fillOpacity: 0.95 },
    volumes:   { radius: 3.0, weight: 0.5, fillOpacity: 0.5 },
  };

  // --- region palette (matches build_data.py / render_heightmap.py) ---
  const SUBREGION_COLORS = {
    'CoralGardens.Shallows':      '#d99fb8',
    'CoralGardens.Plateaus':      '#c46789',
    'CoralGardens.Graveyard':     '#6a4e58',
    'CoralGardens.AnemoneHills':  '#caa253',
    'CoralGardens.TufaTowers':    '#a07a78',
    'CoralGardens.NorthRaceway':  '#a8497a',
    'CoralGardens.SouthRaceway':  '#923c64',
    'CoralGardens.BlightedCoral': '#623a55',
    'CoralGardens.Leadzone':      '#535566',
    'OvergrownRuins.Observatory': '#6ea868',
    'OvergrownRuins.PowerPlant':  '#4a9d8b',
    'OvergrownRuins.RootCanyon':  '#5a7e4b',
  };
  const UNKNOWN_TINT = '#3a4757';

  // ---------- helpers: bilinear sample on the height grid ----------
  function makeHeightSampler(hm) {
    const z = hm.z_cm;
    const W = z[0].length; const H = z.length;
    const b = hm.bounds; const pxCm = hm.pixel_cm;
    return function sampleZ(wx, wy) {
      // Heightmap is bottom-up Y; convert world cm to fractional grid coords.
      const fx = (wx - b.x_min) / pxCm - 0.5;
      const fy = (wy - b.y_min) / pxCm - 0.5;
      let ix0 = Math.floor(fx), iy0 = Math.floor(fy);
      const tx = fx - ix0, ty = fy - iy0;
      const ix1 = ix0 + 1, iy1 = iy0 + 1;
      const cx0 = ix0 < 0 ? 0 : (ix0 >= W ? W - 1 : ix0);
      const cx1 = ix1 < 0 ? 0 : (ix1 >= W ? W - 1 : ix1);
      const cy0 = iy0 < 0 ? 0 : (iy0 >= H ? H - 1 : iy0);
      const cy1 = iy1 < 0 ? 0 : (iy1 >= H ? H - 1 : iy1);
      const z00 = z[cy0][cx0], z10 = z[cy0][cx1];
      const z01 = z[cy1][cx0], z11 = z[cy1][cx1];
      const a = z00 * (1 - tx) + z10 * tx;
      const c = z01 * (1 - tx) + z11 * tx;
      return a * (1 - ty) + c * ty;
    };
  }

  function buildBiomeTintCanvas(D, hm, superSample = 2) {
    // Rasterise biome polygons onto an offscreen canvas at supersampled
    // resolution; later sampled bilinearly by the GridLayer.
    const [H, W] = hm.shape;
    const TW = W * superSample, TH = H * superSample;
    const c = document.createElement('canvas');
    c.width = TW; c.height = TH;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.fillStyle = UNKNOWN_TINT;
    ctx.fillRect(0, 0, TW, TH);
    const b = hm.bounds;
    D.biomes.features.forEach(f => {
      const coords = f.geometry.coordinates[0];
      const color = SUBREGION_COLORS[f.properties.key] || f.properties.color || UNKNOWN_TINT;
      ctx.fillStyle = color;
      ctx.beginPath();
      coords.forEach(([x, y], i) => {
        const cx = (x - b.x_min) / (b.x_max - b.x_min) * TW;
        // World Y points up; canvas Y points down.
        const cy = (1 - (y - b.y_min) / (b.y_max - b.y_min)) * TH;
        if (i === 0) ctx.moveTo(cx, cy); else ctx.lineTo(cx, cy);
      });
      ctx.closePath();
      ctx.fill();
    });
    return ctx.getImageData(0, 0, TW, TH);
  }

  function zStats(hm) {
    const z = hm.z_cm;
    let zMin = Infinity, zMax = -Infinity;
    for (let r = 0; r < z.length; r++) {
      const row = z[r];
      for (let c = 0; c < row.length; c++) {
        const v = row[c];
        if (v < zMin) zMin = v;
        if (v > zMax) zMax = v;
      }
    }
    return { zMin, zMax };
  }

  // ---------- L.GridLayer.Heightmap ----------
  // Custom Leaflet tile layer that samples our heightmap per-tile-pixel and
  // renders hillshade / bathymetry / hillshade+tint on the fly.  Tiles are
  // canvas elements drawn at native screen resolution, so the result stays
  // crisp at any zoom level.
  const HeightmapGridLayer = L.GridLayer.extend({
    options: {
      tileSize: 256,
      mode: 'tint',           // 'tint' | 'shade' | 'depth'
      azDeg: 315,
      altDeg: 32,
      vex: 0.01,
      depthRamp: [
        [4, 14, 36],
        [12, 36, 78],
        [22, 78, 124],
        [40, 154, 170],
        [148, 230, 220],
      ],
      keepBuffer: 4,
      // L.GridLayer defaults minZoom to 0, but we run on L.CRS.Simple with
      // negative zooms (fitBounds lands at z ~= -7).  Without these overrides,
      // Leaflet's layer-control would disable the radio because the layer's
      // declared zoom range excludes the current map zoom.
      minZoom: -20,
      maxZoom: 20,
    },

    initialize(D, options) {
      L.GridLayer.prototype.initialize.call(this, options);
      this._D = D;
      this._sample = makeHeightSampler(D.heightmap);
      this._stats = zStats(D.heightmap);
      // Precompute hillshade once at heightmap resolution so per-tile rendering
      // is just a bilinear sample of an RGBA buffer — no slope math in the
      // inner loop.
      this._rgba = this._buildRGBA();
    },

    _buildRGBA() {
      const hm = this._D.heightmap;
      const z = hm.z_cm;
      const W = z[0].length;
      const H = z.length;
      const pxCm = hm.pixel_cm;
      const opts = this.options;
      const mode = opts.mode;
      const az = (opts.azDeg - 90) * Math.PI / 180;
      const alt = opts.altDeg * Math.PI / 180;
      const lx = Math.cos(alt) * Math.cos(az);
      const ly = Math.cos(alt) * Math.sin(az);
      const lz = Math.sin(alt);
      const vex = opts.vex;
      const ramp = opts.depthRamp;
      const { zMin, zMax } = this._stats;
      const zRange = (zMax - zMin) || 1;

      let tint = null;
      if (mode === 'tint') {
        tint = buildBiomeTintCanvas(this._D, hm, 1);
      }

      const out = new Uint8ClampedArray(W * H * 4);
      for (let r = 0; r < H; r++) {
        for (let c = 0; c < W; c++) {
          const zv = z[r][c];
          const oi = (r * W + c) * 4;

          // Outside the wall polygon: fully transparent.
          if (zv == null) {
            out[oi + 3] = 0;
            continue;
          }

          // Null-tolerant slope (use center value if a neighbour is outside
          // the polygon, so edge cells don't get a runaway gradient).
          const dzdx = ((z[r][Math.min(W - 1, c + 1)] ?? zv) -
                        (z[r][Math.max(0, c - 1)]     ?? zv)) / (2 * pxCm) * vex;
          const dzdy = ((z[Math.min(H - 1, r + 1)][c] ?? zv) -
                        (z[Math.max(0, r - 1)][c]     ?? zv)) / (2 * pxCm) * vex;
          const nLen = Math.sqrt(dzdx * dzdx + dzdy * dzdy + 1);
          let shade = (-dzdx * lx + -dzdy * ly + 1 * lz) / nLen;
          shade = Math.max(0, Math.min(1, 0.18 + 1.4 * shade));

          let R, G, B;
          if (mode === 'shade') {
            R = G = B = shade * 255;
          } else if (mode === 'depth') {
            const t = Math.max(0, Math.min(0.999, (zv - zMin) / zRange));
            const f = t * (ramp.length - 1);
            const i0 = Math.floor(f), i1 = Math.min(ramp.length - 1, i0 + 1);
            const a = f - i0;
            R = ramp[i0][0] * (1 - a) + ramp[i1][0] * a;
            G = ramp[i0][1] * (1 - a) + ramp[i1][1] * a;
            B = ramp[i0][2] * (1 - a) + ramp[i1][2] * a;
          } else {
            const cy = (H - 1 - r);
            const ti = (cy * W + c) * 4;
            R = tint.data[ti]     * shade;
            G = tint.data[ti + 1] * shade;
            B = tint.data[ti + 2] * shade;
          }

          out[oi]     = R;
          out[oi + 1] = G;
          out[oi + 2] = B;
          out[oi + 3] = 255;
        }
      }
      return { data: out, W, H };
    },

    createTile(coords) {
      const size = this.getTileSize();
      const tile = document.createElement('canvas');
      tile.width  = size.x;
      tile.height = size.y;
      const ctx = tile.getContext('2d');

      const map = this._map;
      const pxOriginX = coords.x * size.x;
      const pxOriginY = coords.y * size.y;
      const nw = map.unproject([pxOriginX,          pxOriginY],          coords.z);
      const ne = map.unproject([pxOriginX + size.x, pxOriginY],          coords.z);
      const sw = map.unproject([pxOriginX,          pxOriginY + size.y], coords.z);
      const x0 = nw.lng, y0 = nw.lat;
      const dxPerPx = (ne.lng - nw.lng) / size.x;
      const dyPerPx = (sw.lat - nw.lat) / size.y;

      const img = ctx.createImageData(size.x, size.y);
      const b = this._D.heightmap.bounds;
      const rgba = this._rgba;
      const RW = rgba.W, RH = rgba.H;
      const rdata = rgba.data;
      const xRange = b.x_max - b.x_min;
      const yRange = b.y_max - b.y_min;

      for (let py = 0; py < size.y; py++) {
        const wy = y0 + py * dyPerPx;
        if (wy < b.y_min || wy > b.y_max) {
          // Entire row is outside playable Y; mark transparent.
          for (let px = 0; px < size.x; px++) {
            img.data[(py * size.x + px) * 4 + 3] = 0;
          }
          continue;
        }
        // Nearest-neighbour: each heightmap cell renders as a crisp block.
        const fyN = (wy - b.y_min) / yRange * (RH - 1);
        const iy = Math.max(0, Math.min(RH - 1, Math.round(fyN)));
        const rowOff = iy * RW * 4;
        for (let px = 0; px < size.x; px++) {
          const wx = x0 + px * dxPerPx;
          if (wx < b.x_min || wx > b.x_max) {
            img.data[(py * size.x + px) * 4 + 3] = 0;
            continue;
          }
          const fxN = (wx - b.x_min) / xRange * (RW - 1);
          const ix = Math.max(0, Math.min(RW - 1, Math.round(fxN)));
          const si = rowOff + ix * 4;
          const idx = (py * size.x + px) * 4;
          img.data[idx]     = rdata[si];
          img.data[idx + 1] = rdata[si + 1];
          img.data[idx + 2] = rdata[si + 2];
          img.data[idx + 3] = rdata[si + 3];  // preserve transparency from polygon mask
        }
      }
      ctx.putImageData(img, 0, 0);
      return tile;
    },
  });

  function makeHeightmapLayer(D, mode) {
    return new HeightmapGridLayer(D, { mode });
  }

  // ---------- contour generator (marching squares) ----------
  /**
   * Returns an array of {level, paths: [[ [x,y], ... ]] } from a 2D z grid.
   * Coords are in world UE cm (so they plug straight into L.polyline via the
   * coordsToLatLng path).
   */
  function buildContours(z, bounds, pxCm, levels) {
    const H = z.length, W = z[0].length;
    const result = [];

    function worldFromGrid(c, r) {
      // c,r are fractional grid coords; bottom-up Y (matches z layout).
      return [bounds.x_min + c * pxCm, bounds.y_min + r * pxCm];
    }

    for (const lv of levels) {
      const segs = [];
      // For each grid cell, classify corners vs threshold; emit segments.
      for (let r = 0; r < H - 1; r++) {
        for (let c = 0; c < W - 1; c++) {
          const tl = z[r + 1][c],     tr = z[r + 1][c + 1];
          const bl = z[r][c],         br = z[r][c + 1];
          let code = 0;
          if (bl > lv) code |= 1;
          if (br > lv) code |= 2;
          if (tr > lv) code |= 4;
          if (tl > lv) code |= 8;
          if (code === 0 || code === 15) continue;

          // Linear interpolation along edges.
          const tx = (v0, v1) => (lv - v0) / (v1 - v0 || 1e-9);
          const b_e = [c + tx(bl, br), r];
          const r_e = [c + 1, r + tx(br, tr)];
          const t_e = [c + tx(tl, tr), r + 1];
          const l_e = [c, r + tx(bl, tl)];

          switch (code) {
            case 1: case 14: segs.push([l_e, b_e]); break;
            case 2: case 13: segs.push([b_e, r_e]); break;
            case 3: case 12: segs.push([l_e, r_e]); break;
            case 4: case 11: segs.push([t_e, r_e]); break;
            case 6: case 9:  segs.push([b_e, t_e]); break;
            case 7: case 8:  segs.push([l_e, t_e]); break;
            case 5:
              segs.push([l_e, t_e]); segs.push([b_e, r_e]); break;
            case 10:
              segs.push([l_e, b_e]); segs.push([t_e, r_e]); break;
          }
        }
      }
      const paths = segs.map(seg => seg.map(([c, r]) => worldFromGrid(c, r)));
      result.push({ level: lv, paths });
    }
    return result;
  }

  // ---------- main bootstrap ----------
  function loadData() {
    if (window.SN2_DATA) return Promise.resolve(window.SN2_DATA);
    return Promise.all([
      fetch('data/meta.json').then(r => r.json()),
      fetch('data/markers.geojson').then(r => r.json()),
      fetch('data/biomes.geojson').then(r => r.json()),
      fetch('data/zones.geojson').then(r => r.json()).catch(() => null),
      fetch('data/outline.geojson').then(r => r.json()),
      fetch('data/regions.json').then(r => r.json()),
      fetch('data/resonatables.json').then(r => r.json()).catch(() => []),
      fetch('data/databank.json').then(r => r.json()).catch(() => []),
      fetch('data/items.json').then(r => r.json()).catch(() => []),
      fetch('data/creatures.json').then(r => r.json()).catch(() => []),
    ]).then(([meta, markers, biomes, zones, outline, regions, resonatables, databank, items, creatures]) =>
      ({ meta, markers, biomes, zones, outline, regions, resonatables, databank, items, creatures })
    );
  }

  loadData().then(init).catch(err => {
    console.error('Failed to load data:', err);
    alert('Failed to load map data. See console.');
  });

  // ---------- init ----------
  function init(D) {
    const wb = D.meta.world_bounds;
    const sw = L.latLng(wb.y_min, wb.x_min);
    const ne = L.latLng(wb.y_max, wb.x_max);
    const bounds = L.latLngBounds(sw, ne);
    const widthM = wb.width_m, heightM = wb.height_m;

    // Zoom math (L.CRS.Simple): zoom 0 means 1 latlng unit per pixel.
    // World is ~272 000 cm wide; to fit on a ~1400 px viewport we need
    // 272000 / 1400 ~= 195 units per pixel => zoom ~= -log2(195) ~= -7.6.
    // Push minZoom well past that so the user can always zoom out to see all.
    const map = L.map('map', {
      crs: L.CRS.Simple,
      minZoom: -11, maxZoom: 5,
      zoomSnap: 0.25, zoomDelta: 0.5, wheelPxPerZoomLevel: 80,
      preferCanvas: true,
      maxBounds: bounds.pad(2.0),
      maxBoundsViscosity: 0.0,
      zoomControl: false,
    });
    L.control.zoom({ position: 'bottomright' }).addTo(map);

    map.attributionControl.setPrefix('Subnautica 2 data miner | Leaflet');
    map.attributionControl.addAttribution(
      `World ${fmtNum(widthM, 0)} x ${fmtNum(heightM, 0)} m | build ${D.meta.source_build}`
    );

    // ---------- vector biome layer (the visual foundation now) ----------
    const biomeLayer = L.geoJSON(D.biomes, {
      coordsToLatLng: ([x, y]) => L.latLng(y, x),
      style: f => {
        const color = SUBREGION_COLORS[f.properties.key] || f.properties.color || '#666';
        return {
          color: color,
          weight: 1.5,
          fillColor: color,
          fillOpacity: 0.35,
          opacity: 0.9,
          smoothFactor: 1.2,
        };
      },
      onEachFeature: (f, l) => {
        const key = (f.properties.key || '').replace('.', ' / ');
        l.bindTooltip(key, { sticky: true, direction: 'top' });
        l.on('mouseover', e => e.target.setStyle({ fillOpacity: 0.6, weight: 2.5 }));
        l.on('mouseout',  e => biomeLayer.resetStyle(e.target));
      },
    });

    // Optional alt: authoritative zone polygons (rectangles)
    let zoneLayer = null;
    if (D.zones) {
      zoneLayer = L.geoJSON(D.zones, {
        coordsToLatLng: ([x, y]) => L.latLng(y, x),
        style: f => {
          const color = SUBREGION_COLORS[f.properties.key] || f.properties.color || '#666';
          return {
            color: color, weight: 1, fillColor: color, fillOpacity: 0.22, opacity: 0.65,
            dashArray: '3 3',
          };
        },
      });
    }

    // World outline polygon: prominent border.
    const outlineLayer = L.geoJSON(D.outline, {
      coordsToLatLng: ([x, y]) => L.latLng(y, x),
      style: () => ({
        color: '#6affff', weight: 2.5, fillColor: '#000', fillOpacity: 0,
        dashArray: '8 4', opacity: 0.8,
      }),
    });

    // ---------- client-rendered base layers ----------
    // GridLayer subclasses render each Leaflet tile on demand by sampling the
    // heightmap.z_cm grid bilinearly at the tile's world coordinates.  This
    // stays sharp at every zoom level (no precomputed PNG).
    let baseShadeLayer = null;
    let baseDepthLayer = null;
    let baseReliefLayer = null;
    if (D.heightmap) {
      baseShadeLayer  = makeHeightmapLayer(D, 'tint');
      baseDepthLayer  = makeHeightmapLayer(D, 'depth');
      baseReliefLayer = makeHeightmapLayer(D, 'shade');
    }

    // ---------- depth contour layer ----------
    let contourLayer = null;
    if (D.heightmap) {
      const hm = D.heightmap;
      const [H, W] = hm.shape;
      // Contour every 50 m (5000 cm). Z is negative downward in UE.
      const levels = [];
      for (let depth = 50; depth <= 600; depth += 50) levels.push(-depth * 100);

      const contours = buildContours(hm.z_cm, hm.bounds, hm.pixel_cm, levels);
      const cFeatures = contours.map(c => ({
        type: 'Feature',
        properties: { depth_m: Math.round(-c.level / 100) },
        geometry: { type: 'MultiLineString', coordinates: c.paths },
      }));

      contourLayer = L.geoJSON({ type: 'FeatureCollection', features: cFeatures }, {
        coordsToLatLng: ([x, y]) => L.latLng(y, x),
        style: f => {
          const d = f.properties.depth_m;
          const intensity = Math.min(1, d / 600);
          return {
            color: `rgba(${Math.round(200 + 55 * intensity)}, ${Math.round(220 - 60 * intensity)}, ${Math.round(255 - 100 * intensity)}, 0.5)`,
            weight: (d % 100 === 0) ? 0.9 : 0.5,
          };
        },
        onEachFeature: (f, l) => l.bindTooltip(`${f.properties.depth_m} m`, { sticky: true }),
      });
    }

    // ---------- biome labels ----------
    const biomeLabels = L.layerGroup();
    D.biomes.features.forEach(f => {
      const coords = f.geometry.coordinates[0];
      let cx = 0, cy = 0;
      coords.forEach(([x, y]) => { cx += x; cy += y; });
      cx /= coords.length; cy /= coords.length;
      const key = f.properties.key || '';
      const label = key.split('.').pop().replace(/([a-z])([A-Z])/g, '$1 $2');
      biomeLabels.addLayer(L.marker([cy, cx], {
        icon: L.divIcon({
          className: 'biome-label-wrap',
          html: `<div class="biome-label">${escapeHtml(label)}</div>`,
          iconSize: null,
        }),
        interactive: false,
        keyboard: false,
      }));
    });

    // ---------- per-category canvas marker layers ----------
    // CircleMarkers on a single canvas renderer scale to tens of thousands of
    // points smoothly; no clustering, every placement shown at its exact spot.
    const markerCanvas = L.canvas({ padding: 0.25 });
    const catLayers = {};
    const markersByCat = {};
    ['resources', 'creatures', 'pois', 'loot', 'caves', 'volumes'].forEach(cat => {
      catLayers[cat] = L.layerGroup();
      markersByCat[cat] = [];
    });

    const all = D.markers.features;
    const resoByGroup = {};
    D.resonatables.forEach(r => { if (r.name) resoByGroup[r.name] = r; });
    const databankByTitle = {};
    D.databank.forEach(e => { if (e.title) databankByTitle[e.title.toLowerCase()] = e; });

    const markersById = {};
    all.forEach(f => {
      const props = f.properties;
      const [x, y] = f.geometry.coordinates;
      const style = CAT_STYLE[props.cat] || CAT_STYLE.resources;
      const m = L.circleMarker([y, x], {
        renderer: markerCanvas,
        radius: style.radius,
        weight: style.weight,
        color: '#000',
        fillColor: props.color || '#2dd4bf',
        fillOpacity: style.fillOpacity,
        opacity: 0.85,
      });
      m._featureId = f.id;
      m._props = props;
      m.bindPopup(() => renderPopup(props, x, y), {
        maxWidth: 360, minWidth: 240, autoPanPadding: [40, 40],
      });
      m.bindTooltip(props.name || props.class, { sticky: true, opacity: 0.85 });
      markersById[f.id] = m;
      markersByCat[props.cat].push(m);
    });

    function renderPopup(p, x, y) {
      const depth = (p.depth_m != null) ? `${fmtNum(p.depth_m, 1)} m` : '-';
      const region = p.region || p.sub_region || p.biome || '-';
      const lines = [];
      lines.push(`<h3>${escapeHtml(p.name || p.class)}</h3>`);
      lines.push(`<div class="row"><div class="k">Type</div><div class="v">${escapeHtml(p.group)}</div></div>`);
      lines.push(`<div class="row"><div class="k">Category</div><div class="v">${escapeHtml(p.cat)}</div></div>`);
      lines.push(`<div class="row"><div class="k">Region</div><div class="v">${escapeHtml(region)}</div></div>`);
      lines.push(`<div class="row"><div class="k">Depth</div><div class="v">${escapeHtml(depth)}</div></div>`);
      lines.push(`<div class="row"><div class="k">XY (m)</div><div class="v">${fmtNum(x / 100, 1)}, ${fmtNum(y / 100, 1)}</div></div>`);
      lines.push(`<div class="row"><div class="k">Class</div><div class="v" style="font-family:monospace;font-size:10px;color:#7f9bb3;word-break:break-all">${escapeHtml(p.class)}</div></div>`);

      if (p.cat === 'resources') {
        const reso = resoByGroup[p.group];
        if (reso && reso.contents && reso.contents.length) {
          let html = '<div class="drops"><div class="title">Drops on break</div>';
          reso.contents.forEach(c => {
            const cls = (c.resource_class || '').split('/').pop().replace('.', ' / ').replace(/_C$/, '');
            const chance = (c.drop_chance != null) ? (100 * c.drop_chance).toFixed(0) + '%' : '?';
            html += `<div>- ${escapeHtml(cls)} x${c.num_to_drop || 1} (${chance})</div>`;
          });
          html += '</div>';
          lines.push(html);
        }
      }

      const dbEntry = databankByTitle[(p.name || '').toLowerCase()];
      if (dbEntry && dbEntry.text) {
        const txt = dbEntry.text.slice(0, 360) + (dbEntry.text.length > 360 ? '...' : '');
        lines.push(`<div class="description">${escapeHtml(txt)}</div>`);
      }

      return lines.join('');
    }

    // ---------- sidebar UI ----------
    const sidebar = $('#sidebar');

    function buildCategoryGroups() {
      const wrap = $('#categories');
      const cats = ['caves', 'resources', 'pois', 'creatures', 'loot', 'volumes'];
      cats.forEach(cat => {
        const info = D.meta.legend[cat];
        if (!info) return;
        const el = document.createElement('div');
        el.className = 'cat';
        el.dataset.cat = cat;
        const groups = Object.entries(info.groups);
        const openDefault = (cat === 'caves' || cat === 'resources' || cat === 'pois');

        el.innerHTML = `
          <div class="cat-header${openDefault ? ' open' : ''}">
            <span class="twist">&#9656;</span>
            <span class="label">${escapeHtml(info.label)}</span>
            <span class="count">${info.count}</span>
            <input class="toggle-all" type="checkbox" checked title="show all in category">
          </div>
          <div class="cat-body">
            ${groups.map(([g, gi]) => `
              <label data-group="${escapeHtml(g)}">
                <input type="checkbox" checked>
                <span class="swatch" style="background:${gi.color}"></span>
                <span class="grp">${escapeHtml(g)}</span>
                <span class="num">${gi.count}</span>
              </label>
            `).join('')}
          </div>
        `;
        wrap.appendChild(el);

        el.querySelector('.cat-header').addEventListener('click', e => {
          if (e.target.tagName === 'INPUT') return;
          el.querySelector('.cat-header').classList.toggle('open');
        });
        el.querySelector('.toggle-all').addEventListener('click', e => {
          e.stopPropagation();
          const on = e.target.checked;
          el.querySelectorAll('.cat-body input[type=checkbox]').forEach(cb => { cb.checked = on; });
          applyFilters();
        });
        el.querySelectorAll('.cat-body input[type=checkbox]').forEach(cb => {
          cb.addEventListener('change', applyFilters);
        });
      });
    }

    function buildRegionChips() {
      const wrap = $('#regions-chips');
      const all = document.createElement('span');
      all.className = 'region-chip active';
      all.dataset.key = '__ALL__';
      all.innerHTML = `<span class="sw" style="background:#9aa"></span><span>All</span>`;
      wrap.appendChild(all);

      D.regions.forEach(r => {
        const chip = document.createElement('span');
        chip.className = 'region-chip';
        chip.dataset.key = r.key;
        chip.innerHTML = `<span class="sw" style="background:${r.color}"></span><span>${escapeHtml(r.sub_region)}</span>`;
        chip.title = r.display_name + ' (' + r.key + ')';
        wrap.appendChild(chip);
      });

      wrap.addEventListener('click', e => {
        const chip = e.target.closest('.region-chip');
        if (!chip) return;
        if (chip.dataset.key === '__ALL__') {
          $$('.region-chip', wrap).forEach(c => c.classList.remove('active'));
          chip.classList.add('active');
        } else {
          $('.region-chip[data-key="__ALL__"]', wrap).classList.remove('active');
          chip.classList.toggle('active');
          if (!$$('.region-chip.active', wrap).length) {
            $('.region-chip[data-key="__ALL__"]', wrap).classList.add('active');
          }
        }
        applyFilters();
      });
    }

    function buildDepthSlider() {
      const wrap = $('#depth-slider-wrap');
      const min = Math.max(0, Math.floor(D.meta.depth_range_m.min));
      const max = Math.ceil(D.meta.depth_range_m.max);

      wrap.innerHTML = `
        <div class="row">
          <span>Depth (m)</span>
          <span style="margin-left:auto">
            <span id="depth-min-val">${min}</span> -
            <span id="depth-max-val">${max}</span>
          </span>
        </div>
        <input type="range" id="depth-min" min="${min}" max="${max}" value="${min}">
        <input type="range" id="depth-max" min="${min}" max="${max}" value="${max}">
      `;
      const minSlider = $('#depth-min'), maxSlider = $('#depth-max');
      const minOut = $('#depth-min-val'), maxOut = $('#depth-max-val');
      const onChange = () => {
        let lo = +minSlider.value, hi = +maxSlider.value;
        if (lo > hi) { [lo, hi] = [hi, lo]; }
        minOut.textContent = lo;
        maxOut.textContent = hi;
        applyFilters();
      };
      minSlider.addEventListener('input', onChange);
      maxSlider.addEventListener('input', onChange);
    }

    // ---------- filter logic ----------
    function currentFilters() {
      const enabledGroups = {};
      $$('#categories .cat').forEach(el => {
        const cat = el.dataset.cat;
        enabledGroups[cat] = new Set();
        $$('.cat-body label', el).forEach(lbl => {
          if (lbl.querySelector('input').checked) {
            enabledGroups[cat].add(lbl.dataset.group);
          }
        });
      });
      const activeRegions = new Set();
      $$('#regions-chips .region-chip.active').forEach(c => activeRegions.add(c.dataset.key));
      const allRegions = activeRegions.has('__ALL__');
      const depthMin = +($('#depth-min')?.value ?? -Infinity);
      const depthMax = +($('#depth-max')?.value ?? Infinity);
      const lo = Math.min(depthMin, depthMax);
      const hi = Math.max(depthMin, depthMax);
      const search = ($('#search').value || '').trim().toLowerCase();
      return { enabledGroups, activeRegions, allRegions, lo, hi, search };
    }

    function passes(f, fl) {
      const p = f.properties;
      const eg = fl.enabledGroups[p.cat];
      if (!eg) return false;
      if (!eg.has(p.group)) return false;
      if (!fl.allRegions) {
        const key = p.sub_region ? `${p.biome}.${p.sub_region}` : null;
        if (!key || !fl.activeRegions.has(key)) return false;
      }
      if (p.depth_m != null) {
        if (p.depth_m < fl.lo || p.depth_m > fl.hi) return false;
      }
      if (fl.search) {
        const hay = ((p.name || '') + ' ' + (p.class || '') + ' ' + (p.group || '')).toLowerCase();
        if (hay.indexOf(fl.search) === -1) return false;
      }
      return true;
    }

    function applyFilters() {
      const fl = currentFilters();
      let total = 0;
      Object.keys(catLayers).forEach(cat => {
        const want = [];
        markersByCat[cat].forEach(m => {
          if (passes({ properties: m._props, id: m._featureId }, fl)) {
            want.push(m);
            total++;
          }
        });
        const group = catLayers[cat];
        group.clearLayers();
        want.forEach(m => group.addLayer(m));
      });
      $('#search-count').textContent = `${total} / ${all.length}`;
    }

    // ---------- layer control / scale ----------
    L.control.scale({ position: 'bottomleft', metric: true, imperial: false, maxWidth: 200 }).addTo(map);

    const overlays = {
      'Biome polygons':    biomeLayer,
      'World outline':     outlineLayer,
      'Biome labels':      biomeLabels,
      'Caves':             catLayers.caves,
      'Resources':         catLayers.resources,
      'Creatures':         catLayers.creatures,
      'POIs':              catLayers.pois,
      'Loot / wrecks':     catLayers.loot,
      'Volumes':           catLayers.volumes,
    };
    if (zoneLayer)     overlays['Zone polygons (AABB)'] = zoneLayer;
    if (contourLayer)  overlays['Depth contours (50 m)'] = contourLayer;

    const baseLayers = {};
    if (baseShadeLayer)  baseLayers['Hillshade + biome tint'] = baseShadeLayer;
    if (baseDepthLayer)  baseLayers['Bathymetry (depth)']     = baseDepthLayer;
    if (baseReliefLayer) baseLayers['Hillshade only']         = baseReliefLayer;
    baseLayers['Dark (none)'] = L.layerGroup([]);

    L.control.layers(baseLayers, overlays, { position: 'topright', collapsed: false }).addTo(map);

    // Default on:
    if (baseShadeLayer) baseShadeLayer.addTo(map);
    biomeLayer.addTo(map);
    outlineLayer.addTo(map);
    biomeLabels.addTo(map);
    catLayers.caves.addTo(map);
    catLayers.resources.addTo(map);
    catLayers.creatures.addTo(map);
    catLayers.pois.addTo(map);
    catLayers.loot.addTo(map);

    // ---------- cursor info ----------
    const infoPane = $('#info-pane');
    map.on('mousemove', e => {
      const { lat, lng } = e.latlng;
      infoPane.querySelector('.pos').textContent =
        `X ${fmtNum(lng / 100, 1)} m   Y ${fmtNum(lat / 100, 1)} m`;
    });

    // ---------- search ----------
    let searchT;
    $('#search').addEventListener('input', () => {
      clearTimeout(searchT);
      searchT = setTimeout(applyFilters, 120);
    });

    // ---------- sidebar toggle ----------
    $('#sidebar-toggle').addEventListener('click', () => {
      sidebar.classList.toggle('hidden');
      $('#map').classList.toggle('full');
      $('#sidebar-toggle').classList.toggle('collapsed');
      setTimeout(() => map.invalidateSize(), 220);
    });

    // ---------- meta header ----------
    $('#meta-stats').textContent =
      `${fmtNum(all.length)} placements - ` +
      `${fmtNum(widthM)} x ${fmtNum(heightM)} m - ` +
      `${D.regions.length} regions - ` +
      `depth 0 -> ${Math.round(D.meta.depth_range_m.max)} m`;

    buildCategoryGroups();
    buildRegionChips();
    buildDepthSlider();

    // Fit and let the user zoom further out if they want.
    map.fitBounds(bounds, { padding: [40, 40] });
    applyFilters();

    window.SN2 = { map, D, catLayers, markersById, applyFilters, makeHeightmapLayer };
  }
})();
