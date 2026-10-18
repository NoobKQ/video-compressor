# Video Compressor

A CLI tool for batch video compression using FFmpeg. Reduce file sizes for storage while maintaining quality.

## Features

- **Preset-based compression** — ultrafast to veryslow encoding presets
- **Quality control** — CRF (Constant Rate Factor) 0–51
- **Multiple file handling modes** — keep, overwrite, or backup
- **Parallel processing** — multi-threaded compression with live worker scaling
- **Split-parallel encoding** — split large files into N parts and encode simultaneously
- **GPU acceleration** — NVIDIA, Intel, AMD, or Apple (10–50x faster, 10–20% larger)
- **Size targeting** — `--max-size` auto-picks CRF per file
- **Progress bars** — real-time progress with ETA and colorized results
- **Live controls** — pause, resume, skip, and adjust workers mid-run
- **Smart skip** — detects already compressed files and resumes from the done-log
- **Inspect modes** — preview, summarize, and analyze without modifying files
- **Recursive scanning** — process entire folder trees
- **Config files** — YAML configuration with local overrides
- **Disk-space guard** — stops queueing below a free-space threshold
- **Cleanup** — `--clean` removes leftover temp and part files

## Prerequisites

- Python 3.8+
- FFmpeg installed and available in `PATH`

```bash
# Windows
winget install Gyan.FFmpeg

# macOS
brew install ffmpeg

# Linux
sudo apt install ffmpeg
```

## Installation

Clone the repository and install in editable mode:

```bash
git clone https://github.com/NoobKQ/video-compressor.git
cd video-compressor
pip install -e .
```

Verify the install:

```bash
compressor --help
```

## Usage

### Basic compression

```bash
# Compress all videos in a directory
compressor ./videos

# Custom output directory (keep mode)
compressor ./videos -o ./my-output
```

### Quality and picture

```bash
# High quality (slower, better quality)
compressor ./videos --preset slow --crf 20

# Downscale and cap frame rate
compressor ./videos --scale 720p --fps 30

# Cap each output file to 100 MB (auto-picks CRF)
compressor ./videos --max-size 100
```

### Speed

```bash
# Use GPU encoding
compressor ./videos --gpu auto

# Use 8 parallel workers
compressor ./videos --workers 8

# Split big files into 4 parallel parts
compressor ./videos --split-segments 4
```

### File handling

```bash
# Overwrite originals
compressor ./videos --mode overwrite

# Back up originals before compressing
compressor ./videos --mode backup

# Only process specific extensions
compressor ./videos --ext mp4,mkv

# Skip already compressed files
compressor ./videos --skip-compressed
```

### Inspect without changing files

```bash
# Preview what would happen
compressor ./videos --dry-run

# Summarize the collection
compressor ./videos --info

# Show per-file details
compressor ./videos --info-all

# Analyze savings per option with test encodes
compressor ./videos --analyze

# Clean leftover temp and part files
compressor ./videos --clean
```

### File handling modes

| Mode | Behavior |
|------|----------|
| `keep` | Originals preserved; compressed files go to `./compressed/` |
| `overwrite` | Original files are replaced with compressed versions |
| `backup` | Originals move to `./.backup/`; compressed files replace originals |

### Interactive controls

These keys work live while compressing:

| Key | Action |
|-----|--------|
| `P` | Pause or resume all encodes |
| `S` | Skip the currently running video(s) |
| `[` | Decrease workers |
| `]` | Increase workers |
| `Ctrl+C` | Stop everything and clean up temp files |

### Presets guide

| Preset | Speed | Compression | Use case |
|--------|-------|-------------|----------|
| ultrafast | Fastest | Lowest | Quick tests |
| veryfast | Very fast | Low | Draft processing |
| faster | Fast | Medium | Batch processing |
| fast | Moderate | Good | General use |
| **medium** | **Balanced** | **Good** | **Default** |
| slow | Slow | Better | Quality priority |
| slower | Very slow | High | Archive quality |
| veryslow | Slowest | Highest | Maximum quality |

### CRF guide

| CRF | Quality | File size |
|-----|---------|-----------|
| 16–18 | Visually lossless | Large |
| 20 | High quality | Medium-large |
| **23** | **Good (default)** | **Medium** |
| 26–28 | Decent | Medium-small |
| 30–32 | Acceptable | Small |
| 35+ | Noticeable loss | Very small |

## Configuration

Config files merge in this order. Later files win:

1. Built-in defaults
2. `./config.yaml` (project root)
3. `~/.config/compressor/config.yaml` (user config)
4. `./config.local.yaml` (local overrides, not committed)
5. `~/.config/compressor/config.local.yaml` (global local overrides)

Example `config.yaml`:

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

You can also load a specific file with `--config FILE`.

## Contributing

Contributions are welcome.

1. Fork the repository
2. Create a feature branch: `git checkout -b my-feature`
3. Make your changes
4. Run a quick smoke test: `compressor --help` and `compressor ./sample --dry-run`
5. Commit and open a pull request

[TODO: add a CONTRIBUTING.md with coding style, test commands, and issue templates]

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
