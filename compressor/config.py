"""Configuration loading and merging."""

import yaml
from pathlib import Path
from typing import Any, Dict, Optional
import argparse


DEFAULT_CONFIG = {
    "encoding": {
        "preset": "medium",
        "crf": 23,
        "max_size_mb": None,
        "codec": "libx264",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
        "audio_threshold": None,
        "threads": 0,
        "priority": "below_normal",
    },
    "video": {
        "scale": None,
        "scale_threshold": None,
        "fps": None,
        "fps_threshold": None,
        "keep_aspect": True,
    },
    "processing": {
        "workers": "auto",
        "min_free_space_gb": 0,
        "skip_compressed": True,
        "min_size_mb": 1,
        "extensions": ["mp4", "mov", "mkv", "avi", "webm", "flv", "wmv", "m4v"],
        "split_segments": 0,
        "split_min_size_mb": 500,
    },
    "file_handling": {
        "mode": "keep",
        "backup_dir": ".backup",
        "output_dir": "compressed",
    },
    "gpu": {
        "enabled": False,
        "vendor": "auto",
        "fallback_cpu": True,
    },
    "behavior": {
        "dry_run": False,
        "verbose": False,
        "overwrite_output": False,
    },
}


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load configuration from file."""
    config = DEFAULT_CONFIG.copy()

    # Check default locations
    search_paths = []
    if config_path:
        search_paths.append(config_path)
    search_paths.extend([
        Path.cwd() / ".compressor.yaml",
        Path.cwd() / "config.yaml",
        Path.home() / ".config" / "compressor" / "config.yaml",
    ])

    for path in search_paths:
        if path and path.exists():
            try:
                with open(path) as f:
                    file_config = yaml.safe_load(f)
                    if file_config:
                        config = deep_merge(config, file_config)
                        break
            except Exception:
                pass

    # Personal overrides (never commit these files - see .gitignore).
    # Lets your machine differ from the published defaults, e.g.:
    #   processing: {workers: auto, split_segments: auto, split_min_size_mb: 0}
    # Home applies everywhere (including the installed command); the working
    # directory's file wins when both exist.
    local_paths = [
        Path.home() / ".config" / "compressor" / "config.local.yaml",
        Path.cwd() / "config.local.yaml",
    ]
    for local_path in local_paths:
        if local_path.exists():
            try:
                with open(local_path) as f:
                    local_config = yaml.safe_load(f)
                    if local_config:
                        config = deep_merge(config, local_config)
            except Exception:
                pass

    return config


def merge_configs(config: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """Merge CLI arguments into config."""
    # Encoding
    if args.preset:
        config["encoding"]["preset"] = args.preset
    if args.crf is not None:
        config["encoding"]["crf"] = args.crf
    if args.codec:
        config["encoding"]["codec"] = args.codec
    if args.audio_codec:
        config["encoding"]["audio_codec"] = args.audio_codec
    if args.audio_bitrate:
        config["encoding"]["audio_bitrate"] = args.audio_bitrate
    if args.audio_threshold:
        config["encoding"]["audio_threshold"] = args.audio_threshold

    # Video processing
    if args.scale:
        config["video"]["scale"] = args.scale
    if args.scale_threshold:
        config["video"]["scale_threshold"] = args.scale_threshold
    if args.fps is not None:
        config["video"]["fps"] = args.fps
    if args.fps_threshold is not None:
        config["video"]["fps_threshold"] = args.fps_threshold
    if args.keep_aspect is not None:
        config["video"]["keep_aspect"] = args.keep_aspect

    # Processing
    if getattr(args, "workers", None) is not None:
        config["processing"]["workers"] = args.workers
    if getattr(args, "min_free_space", None):
        config["processing"]["min_free_space_gb"] = args.min_free_space
    if getattr(args, "max_size", None):
        config["encoding"]["max_size_mb"] = args.max_size
    if hasattr(args, "skip_compressed") and args.skip_compressed is not None:
        config["processing"]["skip_compressed"] = args.skip_compressed
    if args.min_size:
        config["processing"]["min_size_mb"] = args.min_size
    if args.split_segments is not None:
        config["processing"]["split_segments"] = args.split_segments
    if args.split_min_size is not None:
        config["processing"]["split_min_size_mb"] = args.split_min_size

    # File handling
    if args.mode:
        config["file_handling"]["mode"] = args.mode
    if args.backup_dir:
        config["file_handling"]["backup_dir"] = args.backup_dir
    if args.output_dir:
        config["file_handling"]["output_dir"] = args.output_dir

    # Behavior
    if args.dry_run:
        config["behavior"]["dry_run"] = True
    if args.verbose:
        config["behavior"]["verbose"] = True

    # GPU
    if args.gpu and args.gpu != "none":
        config["gpu"]["enabled"] = True
        config["gpu"]["vendor"] = args.gpu
    if args.no_gpu_fallback:
        config["gpu"]["fallback_cpu"] = False

    return config


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result