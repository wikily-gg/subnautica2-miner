"""
CUE4Parse provider for Subnautica 2.

Mirrors the ARK miner's setup but targets UE 5.6 and a single
pak / IoStore container set.
"""

from __future__ import annotations

import logging
import os
import sys

from pythonnet import load as _load_clr

_load_clr("coreclr")

import clr  # noqa: E402

import config  # noqa: E402

config.validate()
sys.path.append(config.CUE4PARSE_DLL_DIR)
clr.AddReference("CUE4Parse")

from System import StringComparer  # noqa: E402
from System.IO import SearchOption  # noqa: E402

from CUE4Parse.Compression import OodleHelper  # noqa: E402
from CUE4Parse.FileProvider import DefaultFileProvider  # noqa: E402
from CUE4Parse.MappingsProvider import FileUsmapTypeMappingsProvider  # noqa: E402
from CUE4Parse.UE4.Objects.Core.Misc import FGuid  # noqa: E402
from CUE4Parse.UE4.Versions import EGame, VersionContainer  # noqa: E402

logger = logging.getLogger(__name__)


def create_provider(read_nanite: bool = False, skip_textures: bool = True) -> DefaultFileProvider:
    """Create the CUE4Parse provider.

    Args:
        read_nanite: enable Nanite mesh data loading (needed for mesh
            export of Nanite-only static meshes). Kept off by default
            for cheap JSON extraction.
        skip_textures: skip loading texture data from referenced packages.
            Kept on by default since most extractors only need texture
            path strings, not pixels.
    """
    logger.info("Creating file provider for: %s", config.PAKS_DIR)

    # CUE4Parse ships an oodle bootstrap; reuse the DLL from the ark miner
    # if it isn't present alongside this file.
    oodle_local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               OodleHelper.OodleFileName)
    if not os.path.exists(oodle_local):
        fallback = os.path.join(
            r"C:\Users\profi\WebstormProjects\ark_data_miner",
            OodleHelper.OodleFileName,
        )
        if os.path.exists(fallback):
            oodle_local = fallback
    OodleHelper.Initialize(oodle_local)
    logger.info("Oodle decompression initialised (%s)", oodle_local)

    game = getattr(EGame, config.UE_GAME)
    versions = VersionContainer(game)
    provider = DefaultFileProvider(
        config.PAKS_DIR,
        SearchOption.TopDirectoryOnly,
        versions,
        StringComparer.OrdinalIgnoreCase,
    )

    provider.SkipReferencedTextures = skip_textures
    provider.ReadNaniteData = read_nanite
    provider.ReadShaderMaps = False
    provider.ReadScriptData = True
    # Lazy serialization is OK for JSON-only flows, but mesh + texture
    # extraction needs eager bulk data so reloading from cache returns
    # populated Mips. Toggle it off when textures are needed.
    provider.UseLazyPackageSerialization = skip_textures

    provider.Initialize()
    logger.info("Provider initialised – scanning for containers")

    if config.AES_KEY is not None:
        from CUE4Parse.Encryption.Aes import FAesKey  # noqa: E402

        key = FAesKey(config.AES_KEY)
        mounted = provider.SubmitKey(FGuid(), key)
        logger.info("Mounted %d encrypted containers", mounted)
    else:
        mounted = provider.Mount()
        logger.info("Mounted %d containers (unencrypted)", mounted)

    provider.LoadVirtualPaths()

    if config.MAPPINGS_PATH:
        provider.MappingsContainer = FileUsmapTypeMappingsProvider(
            config.MAPPINGS_PATH
        )
        logger.info("Loaded mappings: %s", config.MAPPINGS_PATH)
    else:
        logger.warning("No mappings file – unversioned properties will not be resolved")

    logger.info("Provider ready – %d files indexed", provider.Files.Count)
    return provider


def collect_dotnet_garbage() -> None:
    from System import GC as DotNetGC  # noqa: E402
    DotNetGC.Collect()
    DotNetGC.WaitForPendingFinalizers()
    DotNetGC.Collect()


def flush_memory() -> None:
    import gc
    gc.collect()
    collect_dotnet_garbage()
