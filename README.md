# Video Compressor

A CLI tool for batch video compression using FFmpeg. Reduce file sizes for storage while maintaining quality.

## Features

- **Preset-based compression** — ultrafast to veryslow encoding presets
- **Quality control** — CRF (Constant Rate Factor) 0–51
- **Multiple file handling modes** — keep originals, overwrite, or backup
- **Parallel processing** — multi-threaded compression with live worker scaling
- **Split-parallel encoding** — split large files into N parts and encode simultaneously
- **GPU acceleration** — NVIDIA / Intel / AMD / Apple (10–50x faster, 10–20% larger)
- **Size targeting** — `--max-size` auto-picks CRF per file
- **Progress bars** — real-time progress with ETA, colorized results
- **Live controls** — pause/resume, skip, adjust workers mid-run
- **Smart skip** — detects already compressed files, resumes from done-log
- **Dry-run & info modes** — preview, summarize, analyze without modifying files
- **Recursive directory scanning** — process entire folder trees
- **Config file support** — YAML configuration with local overrides
- **Disk-space guard** — stop queueing below a free-space threshold
- **Cleanup** — `--clean` removes leftover temp/part files

## Installation

```bash
# Install FFmpeg first (required)
# Windows: winget install Gyan.FFmpeg
# macOS: brew install ffmpeg
# Linux: apt install ffmpeg

# Install the compressor
pip install -e .
```

## Usage

```bash
# Basic usage - compress all videos in directory
compressor ./videos

# Custom output directory (keep mode)
compressor ./videos -o ./compressed

# High quality (slower, better quality)
compressor ./videos --preset slow --crf 20

# Downscale and cap frame rate
compressor ./videos --scale 720p --fps 30

# Overwrite originals
compressor ./videos --mode overwrite

# Backup originals before compressing
compressor ./videos --mode backup

# Preview what would happen
compressor ./videos --dry-run

# Use GPU encoding
compressor ./videos --gpu auto

# Cap each output file to 100 MB (auto-picks CRF)
compressor ./videos --max-size 100

# Use 8 parallel workers
compressor ./videos --workers 8

# Split big files into 4 parallel parts
compressor ./videos --split-segments 4

# Only process specific extensions
compressor ./videos --ext mp4,mkv

# Skip already compressed files
compressor ./videos --skip-compressed

# Inspect videos without compressing
compressor ./videos --info
compressor ./videos --info-all

# Analyze savings per option with test encodes
compressor ./videos --analyze

# Clean leftover temp and part files
compressor ./videos --clean
```

## File Handling Modes

| Mode | Behavior |
|------|----------|
| `keep` | Originals preserved, compressed files in `./compressed/` |
| `overwrite` | Original files replaced with compressed versions |
| `backup` | Originals moved to `./.backup/`, compressed files replace originals |

## Interactive Controls

While compressing, these keys work live:

| Key | Action |
|-----|--------|
| `P` | Pause / resume all encodes |
| `S` | Skip the currently running video(s) |
| `[` | Decrease workers |
| `]` | Increase workers |
| `Ctrl+C` | Stop everything and clean up temp files |

## Configuration

Config files are merged in this order (later wins):

1. Built-in defaults
2. `./config.yaml` (project root)
3. `~/.config/compressor/config.yaml` (user config)
4. `./config.local.yaml` (your local overrides, not committed)
5. `~/.config/compressor/config.local.yaml` (global local overrides)

```yaml
encoding:
  preset: "medium"
  crf: 23
  codec: "libx264"
  audio_codec: "aac"
  audio_bitrate: "128k"

processing:
  workers: 4
  skip_compressed: true
  min_size_mb: 1
  split_segments: 0
  split_min_size_mb: 500
  extensions:
    - "mp4"
    - "mov"
    - "mkv"
    - "avi"

file_handling:
  mode: "keep"
  backup_dir: ".backup"
  output_dir: "compressed"

gpu:
  enabled: false
  vendor: "auto"
  fallback_cpu: true
```

## Presets Guide

| Preset | Speed | Compression | Use Case |
|--------|-------|-------------|----------|
| ultrafast | Fastest | Lowest | Quick tests |
| veryfast | Very fast | Low | Draft processing |
| faster | Fast | Medium | Batch processing |
| fast | Moderate | Good | General use |
| **medium** | **Balanced** | **Good** | **Default** |
| slow | Slow | Better | Quality priority |
| slower | Very slow | High | Archive quality |
| veryslow | Slowest | Highest | Maximum quality |

## CRF Guide

| CRF | Quality | File Size |
|-----|---------|-----------|
| 16–18 | Visually lossless | Large |
| 20 | High quality | Medium-large |
| **23** | **Good (default)** | **Medium** |
| 26–28 | Decent | Medium-small |
| 30–32 | Acceptable | Small |
| 35+ | Noticeable loss | Very small |

## Requirements

- Python 3.8+
- FFmpeg installed and in PATH

## License

MIT
