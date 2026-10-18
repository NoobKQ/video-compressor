"""CLI interface with argument parsing and progress display."""

import argparse
import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from rich.console import Console
from rich.progress import (
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TransferSpeedColumn,
    DownloadColumn,
    TimeElapsedColumn,
)
from rich.text import Text
from rich.markup import escape
from rich.table import Table
from rich.panel import Panel

from compressor.core import VideoCompressor, CompressionResult, ffprobe_json
from compressor.file_handler import FileHandler
from compressor.config import load_config, merge_configs
from compressor.donelog import DoneLog, settings_hash
from compressor.splitter import (
    PARTS_DIR_NAME,
    get_duration,
    get_keyframes,
    pick_cuts,
    split_file,
    concat_files,
)


console = Console()


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        description="Compress batches of videos with FFmpeg.",
        usage="compressor INPUT [OUTPUT] [options]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  compressor ./videos --scale 720p --fps 30 --crf 28
  compressor ./videos --gpu auto --dry-run
  compressor lecture.mp4 --max-size 100
  compressor ./videos --mode backup --min-free-space 20
  compressor ./videos --info-all

CRF guide: 16-18 visually lossless, 23 default, 26-28 balanced,
30-32 small, 35+ very small (0-51, lower means better and bigger).
Preset guide: ultrafast fastest/largest, medium balanced,
slow/veryslow smaller output.

Config files: ./config.yaml, ~/.config/compressor/config.yaml, --config.
Exit codes: 0 success, 1 error, 130 interrupted.""",
    )

    # Positional arguments
    parser.add_argument(
        "input",
        type=Path,
        help="Input file or folder",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help="Output folder (keep mode only)",
    )

    # Quality
    enc_group = parser.add_argument_group("Quality")
    enc_group.add_argument(
        "--preset",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        default="medium",
        help="Trade speed for size. Default: medium.",
    )
    enc_group.add_argument(
        "--crf",
        type=int,
        choices=range(0, 52),
        metavar="0-51",
        default=23,
        help="Set quality (lower means better and bigger). Default: 23.",
    )
    enc_group.add_argument(
        "--codec",
        choices=["libx264", "libx265", "libvpx-vp9", "libsvtav1", "copy"],
        default="libx264",
        help="Set video codec (copy skips re-encoding). Default: libx264.",
    )
    enc_group.add_argument(
        "--audio-codec",
        choices=["aac", "libopus", "copy"],
        default="aac",
        help="Set audio codec. Default: aac.",
    )
    enc_group.add_argument(
        "--audio-bitrate",
        metavar="BITRATE",
        default="128k",
        help="Set audio bitrate, e.g. 64k. Default: 128k.",
    )
    enc_group.add_argument(
        "--audio-threshold",
        metavar="BITRATE",
        default=None,
        help="Copy audio below this bitrate. Default: --audio-bitrate value.",
    )
    enc_group.add_argument(
        "--max-size",
        type=float,
        default=None,
        metavar="MB",
        help="Cap output size per file; auto-pick CRF. Off by default.",
    )

    # Picture
    vid_group = parser.add_argument_group("Picture")
    vid_group.add_argument(
        "--scale",
        metavar="TARGET",
        help="Downscale to 720p, 0.5, or 1280x720. Off by default.",
    )
    vid_group.add_argument(
        "--scale-threshold",
        metavar="HEIGHT",
        default=None,
        help="Skip scaling below this height. Default: --scale value.",
    )
    vid_group.add_argument(
        "--fps",
        type=float,
        metavar="FPS",
        help="Set frame rate, e.g. 30. Off by default.",
    )
    vid_group.add_argument(
        "--fps-threshold",
        type=float,
        metavar="FPS",
        default=None,
        help="Skip fps change below this rate. Default: --fps value.",
    )
    vid_group.add_argument(
        "--keep-aspect",
        action="store_true",
        default=None,
        help="Keep aspect ratio when scaling. Default on.",
    )
    vid_group.add_argument(
        "--no-keep-aspect",
        action="store_false",
        dest="keep_aspect",
        help="Allow stretching to the exact resolution.",
    )

    # Inspect & preview
    info_group = parser.add_argument_group("Inspect")
    info_group.add_argument(
        "--info",
        action="store_true",
        help="Summarize the collection without compressing.",
    )
    info_group.add_argument(
        "--info-all",
        action="store_true",
        help="Show per-file details without compressing.",
    )
    info_group.add_argument(
        "--analyze",
        action="store_true",
        help="Break down savings per option with test encodes.",
    )
    info_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview estimated size and time; change nothing.",
    )

    # Speed
    proc_group = parser.add_argument_group("Speed")
    proc_group.add_argument(
        "--gpu",
        choices=["auto", "nvidia", "intel", "amd", "apple", "none"],
        default="none",
        help="Encode on GPU (10-50x faster, 10-20%% larger). Default: none.",
    )
    proc_group.add_argument(
        "--no-gpu-fallback",
        action="store_true",
        help="Fail when the GPU encoder is missing.",
    )
    proc_group.add_argument(
        "-w", "--workers",
        type=str,
        default=None,
        metavar="N",
        help="Run N jobs at once, or 'auto'. Default: auto.",
    )
    proc_group.add_argument(
        "--split-segments",
        type=int,
        default=0,
        metavar="N",
        help="Split big files into N parallel parts. Default: 0 (off).",
    )
    proc_group.add_argument(
        "--split-min-size",
        type=float,
        default=None,
        metavar="MB",
        help="Split only files above this size. Default: 500.",
    )

    # Files
    file_group = parser.add_argument_group("Files")
    file_group.add_argument(
        "--mode",
        choices=["keep", "overwrite", "backup"],
        default="keep",
        help="Write outputs: keep, overwrite, or backup. Default: keep.",
    )
    file_group.add_argument(
        "--output-dir",
        metavar="DIR",
        default="compressed",
        help="Name the keep-mode output folder. Default: compressed.",
    )
    file_group.add_argument(
        "--backup-dir",
        metavar="DIR",
        default=".backup",
        help="Name the backup folder. Default: .backup.",
    )
    file_group.add_argument(
        "--ext",
        metavar="EXTS",
        default="mp4,mov,mkv,avi,webm,flv,wmv,m4v",
        help="Process these extensions. Default: mp4,mov,mkv,avi,webm,flv,wmv,m4v.",
    )
    file_group.add_argument(
        "--min-size",
        type=float,
        default=1.0,
        metavar="MB",
        help="Skip files below this size. Default: 1.",
    )
    file_group.add_argument(
        "--skip-compressed",
        action="store_true",
        default=True,
        help="Skip already-compressed files. Default on.",
    )
    file_group.add_argument(
        "--no-skip-compressed",
        action="store_false",
        dest="skip_compressed",
        help="Process every file.",
    )
    file_group.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore the done log and reprocess everything.",
    )
    file_group.add_argument(
        "--min-free-space",
        type=float,
        default=None,
        metavar="GB",
        help="Stop queueing below this free space. Off by default.",
    )
    file_group.add_argument(
        "--include-artifacts",
        action="store_true",
        help="Scan output and backup folders too. Off by default.",
    )

    # General
    behav_group = parser.add_argument_group("General")
    behav_group.add_argument(
        "--clean",
        action="store_true",
        help="Delete leftover temp and part files first (asks first).",
    )
    behav_group.add_argument(
        "--yes",
        action="store_true",
        help="Answer yes to all prompts.",
    )
    behav_group.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show FFmpeg output for debugging.",
    )
    behav_group.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="Load a YAML config file.",
    )

    parser.set_defaults(
        skip_compressed=True,
    )

    return parser


def find_video_files(
    input_path: Path,
    extensions: List[str],
    recursive: bool = True,
    exclude_dirs: Optional[List[str]] = None,
) -> List[Path]:
    """Find all video files in input path."""
    files = []
    ext_set = {e.lower().lstrip(".") for e in extensions}
    exclude_dirs = exclude_dirs or []

    if input_path.is_file():
        if input_path.suffix.lower().lstrip(".") in ext_set:
            files.append(input_path)
    elif input_path.is_dir():
        if recursive:
            for ext in ext_set:
                for f in input_path.rglob(f"*.{ext}"):
                    # Skip files in excluded directories below the input
                    # root (the root itself is never excluded, so pointing
                    # at e.g. ./compressed still scans it).
                    try:
                        below = f.relative_to(input_path).parts[:-1]
                    except ValueError:
                        below = f.parts
                    if any(excl in below for excl in exclude_dirs):
                        continue
                    # Skip our own temp/preview artifacts from interrupted runs
                    if f.stem.endswith((".tmp-compress", ".test", ".sizetest", ".tmp-join", ".tmp")):
                        continue
                    files.append(f)
        else:
            for ext in ext_set:
                for f in input_path.glob(f"*.{ext}"):
                    if f.stem.endswith((".tmp-compress", ".test", ".sizetest", ".tmp-join", ".tmp")):
                        continue
                    files.append(f)

    return sorted(files)


def format_size(size: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_time(seconds: float) -> str:
    """Format seconds as clock time (M:SS or H:MM:SS)."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def show_video_info(files: List[Path], summary_only: bool = False):
    """Display video information for each file."""
    from collections import Counter
    
    if summary_only:
        # Aggregate summary
        res_counter = Counter()
        fps_counter = Counter()
        vcodec_counter = Counter()
        acodec_counter = Counter()
        abitrate_counter = Counter()
        total_size = 0
        total_duration = 0
        
        for f in files:
            try:
                probe = ffprobe_json(f)
                if probe is None:
                    continue
                video = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
                audio = next((s for s in probe["streams"] if s["codec_type"] == "audio"), None)
                fmt = probe.get("format", {})
                
                if video:
                    res = f"{video.get('width')}x{video.get('height')}"
                    res_counter[res] += 1
                    fps = f"{eval(video.get('r_frame_rate', '0')):.2f}"
                    fps_counter[fps] += 1
                    vcodec_counter[video.get('codec_name', 'unknown')] += 1
                
                if audio:
                    acodec_counter[audio.get('codec_name', 'unknown')] += 1
                    if audio.get('bit_rate'):
                        abitrate = f"{int(audio.get('bit_rate'))/1000:.0f}kbps"
                        abitrate_counter[abitrate] += 1
                
                total_size += int(fmt.get("size", 0))
                total_duration += float(fmt.get("duration", 0))
                
            except Exception:
                pass
        
        # Print summary - single compact table
        t = Table(title="Video Collection Summary", show_header=True, header_style="bold cyan")
        t.add_column("Category", style="cyan", no_wrap=True)
        t.add_column("Value", style="green")
        t.add_column("Count", style="yellow", justify="right")
        
        # Video section
        t.add_row("VIDEO", "", "")
        for res, count in res_counter.most_common():
            t.add_row("  Resolution", res, str(count))
        for fps, count in fps_counter.most_common():
            t.add_row("  Frame Rate", f"{fps} fps", str(count))
        for codec, count in vcodec_counter.most_common():
            t.add_row("  Codec", codec, str(count))
        
        # Audio section
        t.add_row("AUDIO", "", "")
        for codec, count in acodec_counter.most_common():
            t.add_row("  Codec", codec, str(count))
        for br, count in abitrate_counter.most_common():
            t.add_row("  Bitrate", br, str(count))
        
        # File section
        t.add_row("FILE", "", "")
        t.add_row("  Total", f"{len(files)} files | {format_size(total_size)} | {total_duration/60:.1f}m", "")
        
        console.print(t)
        
    else:
        # Per-file detailed info
        for f in files:
            try:
                probe = ffprobe_json(f)
                if probe is None:
                    continue
                video = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
                audio = next((s for s in probe["streams"] if s["codec_type"] == "audio"), None)
                fmt = probe.get("format", {})
                
                # Single table per file with sections
                t = Table(show_header=False, title=f.name, title_style="bold cyan")
                t.add_column("Section", style="bold cyan", no_wrap=True)
                t.add_column("Property", style="cyan")
                t.add_column("Value", style="green")
                
                # Video
                if video:
                    t.add_row("VIDEO", "Resolution", f"{video.get('width')}x{video.get('height')}")
                    t.add_row("", "Frame Rate", f"{eval(video.get('r_frame_rate', '0')):.2f} fps")
                    t.add_row("", "Codec", video.get("codec_name", "unknown"))
                    t.add_row("", "Profile", video.get("profile", "N/A"))
                
                # Audio
                if audio:
                    t.add_row("AUDIO", "Codec", audio.get("codec_name", "unknown"))
                    t.add_row("", "Bitrate", f"{int(audio.get('bit_rate', 0))/1000:.0f} kbps" if audio.get('bit_rate') else "unknown")
                    t.add_row("", "Sample Rate", f"{audio.get('sample_rate', 'unknown')} Hz")
                    t.add_row("", "Channels", str(audio.get('channels', 'unknown')))
                
                # File
                size = int(fmt.get("size", 0))
                duration = float(fmt.get("duration", 0))
                t.add_row("FILE", "Size", format_size(size))
                t.add_row("", "Duration", f"{duration:.1f}s ({duration/60:.1f}m)")
                t.add_row("", "Bitrate", f"{int(fmt.get('bit_rate', 0))/1000:.0f} kbps" if fmt.get('bit_rate') else "unknown")
                
                console.print(t)
                
            except Exception as e:
                console.print(f"[red]Error probing {f.name}: {e}[/red]")


def print_summary(results: List[CompressionResult], dry_run: bool = False, workers: int = 1, total_elapsed: Optional[float] = None):
    """Print compression summary."""
    total_original = sum(r.original_size for r in results if not r.skipped)
    total_compressed = sum(r.compressed_size or 0 for r in results if r.success and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if not r.success and not r.skipped)
    success = sum(1 for r in results if r.success and not r.skipped)

    table = Table(title="Compression Summary" + (" (DRY RUN)" if dry_run else ""))
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Files Processed", str(success))
    table.add_row("Files Skipped", str(skipped))
    table.add_row("Files Failed", str(failed))
    table.add_row("Original Size", format_size(total_original))
    table.add_row("Compressed Size", format_size(total_compressed))

    if total_original > 0:
        savings = total_original - total_compressed
        pct = (savings / total_original) * 100
        table.add_row("Space Saved", f"{format_size(savings)} ({pct:.1f}%)")

    if total_elapsed is not None:
        table.add_row("Total Time", format_time(total_elapsed))

    if dry_run:
        # Estimate total time from individual estimates
        total_estimated_time = sum(r.estimated_time or 0 for r in results if r.success and not r.skipped and r.estimated_time)
        if total_estimated_time > 0:
            table.add_row("Est. Time (1 worker)", format_time(total_estimated_time))
            if workers and workers > 1:
                table.add_row(f"Est. Time ({workers} workers)", format_time(total_estimated_time / workers))

    console.print(table)

    # Show failures
    if failed > 0:
        console.print("\n[red]Failed files:[/red]")
        for r in results:
            if not r.success and not r.skipped:
                console.print(f"  [red][FAIL][/red] {r.input_path.name}: {r.error}")


def run_analysis(files: List[Path], config: dict, args: argparse.Namespace, actual_results: List = None):
    """Run analysis to break down space savings by each option.
    If actual_results provided, uses actual compression sizes for final step.
    """
    from compressor.core import VideoCompressor, CompressionResult
    from compressor.file_handler import FileHandler
    import time
    
    console.print("\n[bold cyan]Running analysis...[/bold cyan]")
    
    # Test configurations to try
    # All use CRF 23 (default) except the final step
    target_crf = config.get("encoding", {}).get("crf", 23)
    
    test_configs = [
        ("Baseline (no changes)", {
            "video": {"scale": None, "fps": None},
            "encoding": {"audio_codec": "aac", "audio_bitrate": "128k", "crf": 23}
        }),
    ]
    
    # Add scale test if scale is set
    if config.get("video", {}).get("scale"):
        test_configs.append(("Scale only", {
            "video": {"scale": config["video"]["scale"], "fps": None},
            "encoding": {"audio_codec": "aac", "audio_bitrate": "128k", "crf": 23}
        }))
    
    # Add scale + fps
    if config.get("video", {}).get("scale") and config.get("video", {}).get("fps"):
        test_configs.append(("Scale + FPS", {
            "video": {"scale": config["video"]["scale"], "fps": config["video"]["fps"]},
            "encoding": {"audio_codec": "aac", "audio_bitrate": "128k", "crf": 23}
        }))
    
    # Add scale + fps + audio
    if (config.get("video", {}).get("scale") and config.get("video", {}).get("fps") and 
        (config.get("encoding", {}).get("audio_codec") != "aac" or config.get("encoding", {}).get("audio_bitrate") != "128k")):
        test_configs.append(("Scale + FPS + Audio", {
            "video": {"scale": config["video"]["scale"], "fps": config["video"]["fps"]},
            "encoding": {
                "audio_codec": config["encoding"]["audio_codec"],
                "audio_bitrate": config["encoding"]["audio_bitrate"],
                "crf": 23
            }
        }))
    
    # Add scale + fps + audio + target CRF
    if target_crf != 23:
        test_configs.append(("Scale + FPS + Audio + CRF", {
            "video": {"scale": config["video"]["scale"], "fps": config["video"]["fps"]},
            "encoding": {
                "audio_codec": config["encoding"].get("audio_codec", "aac"),
                "audio_bitrate": config["encoding"].get("audio_bitrate", "128k"),
                "crf": target_crf
            }
        }))
    elif len(test_configs) < 3:
        test_configs.append(("Full config", config.get("video", {})))
    
    # Remove duplicates and ensure full config is last
    seen = set()
    unique_configs = []
    for name, cfg in test_configs:
        if name not in seen:
            seen.add(name)
            unique_configs.append((name, cfg))
    
    # Ensure "Full config" is last
    full_config = None
    for i, (name, cfg) in enumerate(unique_configs):
        if name == "Full config" or (cfg == config.get("video", {}) and len(unique_configs) > 1):
            full_config = (name, cfg)
            unique_configs.pop(i)
            break
    if full_config:
        unique_configs.append(full_config)
    
    results = []
    compressor = VideoCompressor(config)
    file_handler = FileHandler(config)
    
    # Use first file for analysis (representative)
    test_file = files[0]
    output_dir = args.input / config["file_handling"]["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path, _ = file_handler.prepare_output(test_file, output_dir)
    
    console.print(f"[cyan]Testing with: {test_file.name}[/cyan]\n")
    
    for name, test_config in unique_configs:
        console.print(f"  [cyan]Testing {name}...[/cyan]")
        
        # For the final step (actual config), use actual results if provided
        is_final = (name == unique_configs[-1][0])
        use_actual = actual_results is not None and is_final
        
        if use_actual:
            console.print(f"  [cyan]Analyzing actual results...[/cyan]")
            # Find the result for this file
            actual_result = None
            for r in actual_results:
                if r.input_path == test_file:
                    actual_result = r
                    break
            
            if actual_result and actual_result.success and actual_result.compressed_size:
                results.append((name, actual_result.compressed_size, 0))
            else:
                results.append((name, None, 0))
        else:
            # Run dry-run test
            test_cfg = deep_merge(config.copy(), test_config)
            test_compressor = VideoCompressor(test_cfg)
            
            start = time.time()
            result = test_compressor.compress(test_file, output_path, dry_run=True)
            elapsed = time.time() - start
            
            if result.success and result.compressed_size:
                results.append((name, result.compressed_size, elapsed))
            else:
                results.append((name, None, elapsed))
            console.print(f"  [green]Done ({elapsed:.1f}s)[/green]")
    
    # Display breakdown
    console.print()
    t = Table(title="Space Savings Breakdown", show_header=True, header_style="bold cyan")
    t.add_column("Step", style="cyan", width=30)
    t.add_column("Size", style="green", justify="right")
    t.add_column("Saved", style="yellow", justify="right")
    t.add_column("Reduction", style="magenta", justify="right")
    t.add_column("Time", style="blue", justify="right")
    
    baseline_size = None
    for i, (name, size, elapsed) in enumerate(results):
        if size is None:
            t.add_row(name, "FAILED", "-", "-", f"{elapsed:.1f}s")
            continue
        
        if baseline_size is None:
            baseline_size = size
            t.add_row(name, format_size(size), "--", "--", f"{elapsed:.1f}s")
        else:
            saved = baseline_size - size
            pct = (saved / baseline_size) * 100 if baseline_size > 0 else 0
            t.add_row(name, format_size(size), format_size(saved), f"{pct:.1f}%", f"{elapsed:.1f}s")
    
    console.print(t)
    
    # Summary
    if baseline_size and results[-1][1]:
        total_saved = baseline_size - results[-1][1]
        total_pct = (total_saved / baseline_size) * 100
        console.print(f"\n[bold]Total: {format_size(baseline_size)} -> {format_size(results[-1][1])} "
                     f"({format_size(total_saved)} saved, {total_pct:.1f}%)[/bold]")



def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def cleanup_split_parts(parent: dict) -> None:
    """Delete a split parent's segment files and staging artifacts."""
    for seg_in, seg_out in parent.get("segments", []):
        for p in (seg_in, seg_out):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    out = parent.get("out_path")
    for extra in (parent.get("parts_dir") / f"{parent.get('tag')}.concat.txt"
                  if parent.get("parts_dir") and parent.get("tag") else None,
                  out.with_name(out.stem + ".tmp-join.mp4") if out is not None else None):
        if extra is None:
            continue
        try:
            extra.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        parent["parts_dir"].rmdir()
    except (OSError, KeyError):
        pass


def finish_split_parent(parent: dict, file_handler) -> CompressionResult:
    """Join a split file's encoded segments; failed result on any problem.

    Always cleans up segment files. Runs the normal finalize so backup/keep
    handling matches whole-file runs.
    """
    orig = parent["orig"]
    try:
        seg_outs = [seg_out for _, seg_out in parent["segments"]]
        def _bad(p):
            try:
                return not p.exists() or p.stat().st_size == 0
            except OSError:
                return True
        missing = [p for p in seg_outs if _bad(p)]
        if missing:
            seg_errors = [r.error for r in parent["seg_results"]
                          if not r.success and r.error]
            detail = seg_errors[0] if seg_errors else "segment output missing"
            raise RuntimeError(f"{len(missing)}/{len(seg_outs)} parts failed: {detail}")
        dst = parent["out_path"]
        if dst.exists() and dst.resolve() == orig.resolve():
            # Same-path output (overwrite mode): join to temp, then replace.
            tmp = dst.with_name(dst.stem + ".tmp-join.mp4")
            concat_files(seg_outs, tmp, parent["tag"])
            tmp.replace(dst)
        else:
            concat_files(seg_outs, dst, parent["tag"])
        skips = next((r.skipped_methods for r in parent["seg_results"]
                      if r.skipped_methods), None)
        result = CompressionResult(
            input_path=orig,
            output_path=dst,
            original_size=parent["original_size"],
            compressed_size=dst.stat().st_size,
            success=True,
            skipped_methods=skips,
        )
        return file_handler.finalize(result, parent["backup_path"], orig)
    except Exception as e:
        return CompressionResult(
            input_path=orig,
            output_path=parent["out_path"],
            original_size=parent["original_size"],
            compressed_size=None,
            success=False,
            error=f"split join failed: {e}",
        )
    finally:
        cleanup_split_parts(parent)


STAGING_PATTERNS = ("*.tmp-compress.mp4", "*.tmp-join.mp4", "*.test.mp4",
                     "*.sizetest.mp4", "*.tmp.mp4", "*.concat.txt")

ACTION_FLAGS = ("--scale", "--fps", "--crf", "--preset", "--codec",
                "--audio-codec", "--audio-bitrate", "--audio-threshold",
                "--scale-threshold", "--fps-threshold", "--workers", "-w",
                "--mode", "--dry-run", "--analyze", "--info", "--info-all",
                "--gpu", "--split-segments", "--fresh")


def collect_staging(root: Path) -> List[Path]:
    """Find leftover staging files (temps, split parts, concat lists)."""
    if root.is_file():
        root = root.parent
    found: List[Path] = []
    for pattern in STAGING_PATTERNS:
        found.extend(p for p in root.rglob(pattern) if p.is_file())
    for d in root.rglob(PARTS_DIR_NAME):
        if d.is_dir():
            found.extend(p for p in d.rglob("*") if p.is_file())
    seen = set()
    unique = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return sorted(unique)


class AverageETAColumn(ProgressColumn):
    """ETA from the task's overall average speed (not recent speed).

    Rich's TimeRemainingColumn divides by a short-window speed that
    collapses at every file boundary, so the estimate spikes each time
    a video finishes. The lifetime average barely moves instead.
    """

    def render(self, task) -> Text:
        if not str(task.description).startswith("Overall"):
            # Per-file bars keep Rich's classic recent-speed estimate.
            remaining = task.time_remaining
            if remaining is None:
                return Text("--:--", style="progress.remaining")
            eta = int(remaining)
            h, rem = divmod(eta, 3600)
            m, s = divmod(rem, 60)
            text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            return Text(text, style="progress.remaining")
        elapsed = task.elapsed or 0.0
        if task.total is None or task.completed <= 0 or elapsed < 2:
            return Text("--:--", style="progress.remaining")
        avg = task.completed / elapsed
        remaining_bytes = (task.total or 0) - task.completed
        if remaining_bytes <= 0 or avg <= 0:
            return Text("0:00", style="progress.remaining")
        eta = int(remaining_bytes / avg)
        h, rem = divmod(eta, 3600)
        m, s = divmod(rem, 60)
        text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        return Text(text, style="progress.remaining")


def resolve_workers(raw, gpu_enabled: bool) -> int:
    """Turn a --workers value (N or 'auto'/None) into a worker count."""
    if raw is not None and str(raw).strip().lower() not in ("auto", "none", ""):
        try:
            n = int(str(raw).strip())
            if n >= 1:
                return n
        except (TypeError, ValueError):
            pass
    cpu = os.cpu_count() or 4
    if gpu_enabled:
        return max(4, min(8, cpu))
    return max(2, min(8, cpu // 2))


def keep_output_dir_for(args_input: Path, output_dir: Path, f: Path) -> Path:
    """Mirror the input tree inside the keep-mode output dir.

    <in>/a/b/v.mp4 -> <out>/a/b/v.mp4. Files directly in <in> (or a
    single-file input) go straight into <out>/.
    """
    try:
        rel = f.parent.relative_to(args_input)
    except ValueError:
        return output_dir
    return output_dir if str(rel) == "." else output_dir / rel


def run_compression(args: argparse.Namespace) -> int:
    """Main compression workflow."""
    # Load and merge configs
    config = load_config(args.config)
    config = merge_configs(config, args)

    # Resolve worker count once (N or auto based on CPU/GPU mode)
    workers = resolve_workers(config["processing"].get("workers"),
                              config["gpu"].get("enabled", False))
    if str(config["processing"].get("workers", "")).strip().lower() == "auto":
        kind = "GPU" if config["gpu"].get("enabled", False) else "CPU"
        console.print(f"[dim]Workers: auto -> {workers} ({os.cpu_count() or '?'} logical CPUs, {kind} mode)[/dim]")

    # Initialize components
    compressor = VideoCompressor(config)
    file_handler = FileHandler(config)

    # Staging cleanup runs first when requested
    if args.clean:
        targets = collect_staging(args.input)
        if targets:
            try:
                base = args.input if args.input.is_dir() else args.input.parent
            except OSError:
                base = args.input
            total = 0
            console.print(f"[cyan]Staging cleanup for {args.input}:[/cyan]")
            for p in targets:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                total += size
                try:
                    shown = p.relative_to(base)
                except (OSError, ValueError):
                    shown = p.name
                console.print(f"  {shown} ({format_size(size)})")
            if args.yes:
                confirmed = True
            elif not sys.stdin.isatty():
                console.print("[red]--clean needs confirmation: rerun in a terminal or pass --yes.[/red]")
                return 1
            else:
                answer = input(f"Delete {len(targets)} file(s) ({format_size(total)})? [y/N]: ").strip().lower()
                confirmed = answer in ("y", "yes")
            if confirmed:
                for p in targets:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                for d in args.input.rglob(PARTS_DIR_NAME) if args.input.is_dir() else []:
                    try:
                        d.rmdir()
                    except OSError:
                        pass
                console.print(f"[green]Deleted {len(targets)} staging file(s).[/green]")
            else:
                console.print("[dim]Kept staging files.[/dim]")
        else:
            console.print("[dim]No staging files found.[/dim]")
        if not any(s == a or s.startswith(a + "=") for s in sys.argv for a in ACTION_FLAGS):
            return 0

    # Find files
    extensions = args.ext.split(",") if args.ext else config["processing"]["extensions"]
    console.print(f"[cyan]Scanning for videos in {args.input}...[/cyan]")
    
    # Skip our own artifact folders (outputs, backups, split parts) so
    # previous runs are never reprocessed. --include-artifacts lifts this
    # for outputs/backups (parts stay excluded: they are temp segments).
    exclude_dirs = [PARTS_DIR_NAME]
    if not args.include_artifacts:
        names = {
            config["file_handling"]["output_dir"],
            config["file_handling"]["backup_dir"],
        }
        if args.output:
            names.add(Path(args.output).name)
        exclude_dirs.extend(n for n in names if n)
    
    files = find_video_files(args.input, extensions, recursive=True, exclude_dirs=exclude_dirs)

    if not files:
        console.print("[yellow]No video files found![/yellow]")
        return 0

    # Info mode - show video properties and exit
    if args.info or args.info_all:
        show_video_info(files, summary_only=args.info)
        return 0

    console.print(f"[green]Found {len(files)} video file(s)[/green]")
    console.print(f"[dim]PID: {os.getpid()} - force-kill from another terminal: taskkill /PID {os.getpid()} /F[/dim]")

    # Show GPU info if enabled
    if config.get("gpu", {}).get("enabled", False):
        from compressor.gpu import detect_gpu
        gpu_info = detect_gpu()
        if gpu_info.vendor.value != "none":
            console.print(f"[cyan]GPU detected: {gpu_info.name}[/cyan]")
            console.print(f"[cyan]Available encoders: {', '.join(gpu_info.encoders)}[/cyan]")
        else:
            console.print("[yellow]No GPU encoder found, falling back to CPU[/yellow]")

    # Prepare output directory for keep mode
    output_dir = args.output
    if config["file_handling"]["mode"] == "keep" and not output_dir:
        base = args.input if args.input.is_dir() else args.input.parent
        output_dir = base / config["file_handling"]["output_dir"]

    # Prepare tasks (keep mode mirrors the input tree under output_dir)
    keep_mode = config["file_handling"]["mode"] == "keep"
    tasks = []
    for f in files:
        file_outdir = keep_output_dir_for(args.input, output_dir, f) if keep_mode else output_dir
        output_path, backup_path = file_handler.prepare_output(f, file_outdir)
        tasks.append((f, output_path, backup_path))

    # Dry run preview
    if args.dry_run:
        console.print(Panel("[bold yellow]DRY RUN MODE - No files will be modified[/bold yellow]"))
        preview_results = []
        for input_path, output_path, _ in tasks:
            result = compressor.compress(input_path, output_path, dry_run=True)
            preview_results.append(result)
        print_summary(preview_results, dry_run=True, workers=workers)
        
        # Analyze after dry-run if requested
        if args.analyze:
            run_analysis(files, config, args, actual_results=preview_results)
        return 0

    # Actual compression with progress

    # Prepare output directory for keep mode
    output_dir = args.output
    if config["file_handling"]["mode"] == "keep" and not output_dir:
        base = args.input if args.input.is_dir() else args.input.parent
        output_dir = base / config["file_handling"]["output_dir"]

    # Prepare tasks (keep mode mirrors the input tree under output_dir)
    keep_mode = config["file_handling"]["mode"] == "keep"
    tasks = []
    for f in files:
        file_outdir = keep_output_dir_for(args.input, output_dir, f) if keep_mode else output_dir
        output_path, backup_path = file_handler.prepare_output(f, file_outdir)
        tasks.append((f, output_path, backup_path))

    # Dry run preview
    if args.dry_run:
        console.print(Panel("[bold yellow]DRY RUN MODE - No files will be modified[/bold yellow]"))
        preview_results = []
        for input_path, output_path, _ in tasks:
            result = compressor.compress(input_path, output_path, dry_run=True)
            preview_results.append(result)
        print_summary(preview_results, dry_run=True, workers=workers)
        return 0

    # Resume: skip files already finished with identical settings.
    # The log lives next to the input; heuristic skips are never logged,
    # so flag changes (e.g. --no-skip-compressed) still take effect.
    results: List[CompressionResult] = []
    log_root = args.input if args.input.is_dir() else args.input.parent
    done_log = DoneLog(log_root)
    current_settings = settings_hash(config)
    ndone = 0
    if not args.fresh:
        kept = []
        for f, out_path, backup_path in tasks:
            if done_log.is_done(f, current_settings):
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                results.append(CompressionResult(
                    input_path=f, output_path=None, original_size=size,
                    compressed_size=None, success=True, skipped=True,
                    skip_reason="already done with these settings (see .compressor-done.json, use --fresh to redo)",
                ))
                ndone += 1
            else:
                kept.append((f, out_path, backup_path))
        tasks = kept
        if ndone:
            console.print(f"  [dim]Already done: {ndone} file(s) - use --fresh to redo[/dim]")
        if not tasks:
            print_summary(results, total_elapsed=0.0)
            return 0

    # Segment-parallel encoding for big files (dry-run previews whole files)
    split_raw = config["processing"].get("split_segments", 0) or 0
    if str(split_raw).strip().lower() == "auto":
        split_n = max(2, workers // 2)
        console.print(f"[dim]Split: auto -> {split_n} segments ({workers} workers)[/dim]")
    else:
        try:
            split_n = int(split_raw)
        except (TypeError, ValueError):
            split_n = 0
    split_min_bytes = (config["processing"].get("split_min_size_mb", 500) or 0) * 1024 * 1024
    segment_parents = {}

    def try_plan_split(f, out_path, backup_path):
        """Plan a split (cuts only, no files yet); None = handle whole."""
        try:
            size = f.stat().st_size
        except OSError:
            return None
        if size < split_min_bytes:
            return None
        if config["processing"].get("skip_compressed", True):
            skip, _ = compressor.is_already_compressed(f)
            if skip:
                return None
        info = compressor.get_video_info(f)
        duration = info.get("duration", 0) if "error" not in info else 0
        if not duration or duration <= 0:
            return None
        cuts = pick_cuts(duration, get_keyframes(f), split_n)
        if not cuts:
            return None
        parts_dir = f.parent / PARTS_DIR_NAME
        tag = f"{f.stem}_{f.suffix.lstrip('.') or 'nov'}"
        seg_srcs = [parts_dir / f"{tag}_src{i:02d}{f.suffix}"
                    for i in range(len(cuts) + 1)]
        segments = [(seg_in, parts_dir / f"{tag}_enc{i:02d}.mp4")
                    for i, seg_in in enumerate(seg_srcs)]
        parent = {
            "orig": f, "out_path": out_path, "backup_path": backup_path,
            "original_size": size, "segments": segments, "seg_results": [],
            "start": time.monotonic(), "parts_dir": parts_dir, "tag": tag,
            "cuts": cuts, "duration": duration,
        }
        for seg_in, _ in segments:
            segment_parents[seg_in] = parent
        console.print(f"  [cyan][SPLIT][/cyan] {f.name} - {len(segments)} parts ({format_time(duration)})")
        return parent

    unsplit = []
    if split_n >= 2:
        expanded = []
        for f, out_path, backup_path in tasks:
            parent = try_plan_split(f, out_path, backup_path)
            if parent is None:
                expanded.append((f, out_path, backup_path))
            else:
                unsplit.append(parent)
        tasks = expanded

    # Every result line shows done/total; each append is one finished unit.
    total_units = len(tasks) + len(unsplit) + ndone

    # Actual compression with progress
    run_start = time.monotonic()
    stop_flag = threading.Event()
    paused = threading.Event()
    verbose = args.verbose or config.get("behavior", {}).get("verbose", False)

    console.print("[dim]Keys: P pause/resume - S skip a video - [ ] workers - Ctrl+C stop[/dim]")

    # Transient UI lines: ack messages (Pausing..., Workers: x/y, ...) are
    # written on the line below the live bars (no newline) and reclaimed
    # when their action completes - no timers, no line counting. Any other
    # print reclaims the line first (the \r\ESC[2K prefix), so the ack can
    # never strand a half-overwritten line. Raw ANSI is exactly as safe as
    # Rich's own Live rendering, and skipped entirely when redirected.
    ui_lock = threading.Lock()
    ui_state = {"gen": 0, "intact": False, "live": False, "vt": console.is_terminal}

    def _reclaim_locked():
        if ui_state["vt"]:
            try:
                console.file.write("\r\x1b[2K")
            except Exception:
                pass

    def live_print(*args, **kwargs):
        with ui_lock:
            ui_state["intact"] = False
            _reclaim_locked()
            console.print(*args, **kwargs)

    def count_str() -> str:
        return f"{len(results)}/{total_units}"

    def nm(name: str) -> str:
        """Video name in cyan (escaped so [brackets] never parse as markup)."""
        return f"[cyan]{escape(name)}[/cyan]"

    def transient(message, linger=1.5):
        """Show an ack tied to an action. linger=None erases only on .done()."""
        with ui_lock:
            ui_state["gen"] += 1
            gen = ui_state["gen"]
            _reclaim_locked()
            if ui_state["vt"]:
                console.print(message, end="", highlight=False)
                try:
                    console.file.flush()
                except Exception:
                    pass
                ui_state["intact"] = True
            else:
                console.print(message, highlight=False)
                ui_state["intact"] = False
        class _Handle:
            def done(self):
                with ui_lock:
                    if gen != ui_state["gen"] or not ui_state["intact"]:
                        return
                    ui_state["intact"] = False
                    if ui_state["live"] and ui_state["vt"]:
                        try:
                            # Clear the ack line and delete it, pulling the
                            # cursor back up. Bars above are untouched.
                            console.file.write("\r\x1b[2K\x1b[M")
                            console.file.flush()
                        except Exception:
                            pass
        handle = _Handle()
        if linger is not None:
            def _expire():
                time.sleep(linger)
                handle.done()
            threading.Thread(target=_expire, daemon=True).start()
        return handle

    def settle_ui():
        """Withdraw any displayed ack (no-op otherwise).

        Must run before any task add/remove: Rich anchors its redraw on
        the cursor, so region changes under an ack line smear the bars.
        """
        with ui_lock:
            ui_state["gen"] += 1
            if ui_state["intact"]:
                ui_state["intact"] = False
                if ui_state["live"] and ui_state["vt"]:
                    try:
                        console.file.write("\r\x1b[2K\x1b[M")
                        console.file.flush()
                    except Exception:
                        pass

    # Armed ack handles: erased when their action reports back.
    skip_ack = {"key": None, "handle": None}

    def fire_skip_ack(key):
        h = skip_ack.get("handle")
        if h is not None and skip_ack.get("key") == key:
            skip_ack["key"] = None
            skip_ack["handle"] = None
            h.done()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        AverageETAColumn(),
        TransferSpeedColumn(),
        DownloadColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        overall_total = 0
        for f, _, _ in tasks:
            try:
                overall_total += f.stat().st_size
            except OSError:
                pass
        for parent in unsplit:
            overall_total += parent["original_size"]
        if overall_total <= 0:
            overall_total = len(tasks)
        overall_task = progress.add_task("Overall", total=overall_total)
        ui_state["live"] = True
        file_tasks = {}
        file_start_times = {}
        file_sizes = {}
        file_reported = {}
        progress_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Only keep up to `workers` files active at once; the rest wait
            # in a pending queue and start as active ones finish. A background
            # splitter thread cuts big files while encodes run, feeding the
            # same queue, so splitting and compressing overlap.
            future_to_task = {}
            pending = list(tasks)
            events: queue.Queue = queue.Queue()

            pause_ranges = []

            def paused_overlap(a: float, b: float) -> float:
                """Seconds of [a, b] spent paused (excluded from durations)."""
                now = time.monotonic()
                total = 0.0
                for s, e in pause_ranges:
                    total += max(0.0, min(b, now if e is None else e) - max(a, s))
                return total

            def toggle_pause():
                # Acknowledge FIRST so the user knows the keypress landed,
                # even if the suspend/resume work takes a moment.
                if paused.is_set():
                    ack = transient("  [green]Resuming...[/green]", linger=None)
                    now = time.monotonic()
                    if pause_ranges and pause_ranges[-1][1] is None:
                        pause_ranges[-1][1] = now
                    compressor.pause_mark(False)
                    paused.clear()
                    # Unfreeze Rich's per-task clocks (stop_time=None resumes).
                    # pause_ranges still excludes the gap from our own math.
                    with progress._lock:
                        for t in progress.tasks:
                            t.stop_time = None
                    progress.update(overall_task, description="Overall")
                    def _resume():
                        try:
                            compressor.resume_all()
                        finally:
                            ack.done()
                    threading.Thread(target=_resume, daemon=True).start()
                else:
                    ack = transient("  [yellow]Pausing...[/yellow]", linger=None)
                    pause_ranges.append([time.monotonic(), None])
                    compressor.pause_mark(True)
                    paused.set()
                    # Freeze elapsed/ETA instantly; the syscalls follow.
                    with progress._lock:
                        for t in progress.tasks:
                            try:
                                progress.stop_task(t.id)
                            except Exception:
                                pass
                    progress.update(overall_task, description="Overall (PAUSED)")
                    def _suspend():
                        try:
                            compressor.suspend_all()
                        finally:
                            ack.done()
                    threading.Thread(target=_suspend, daemon=True).start()

            selecting = {"options": None, "digits": "", "expires": 0.0}
            slots = {"max": workers}
            user_skipped = set()
            parent_skipped = set()

            def list_active():
                """Snapshot of currently compressing videos (split parents once)."""
                try:
                    items = list(future_to_task.items())
                except RuntimeError:
                    return None
                options = []
                seen = set()
                for _, (inp, _o, _b, _t) in items:
                    sp = segment_parents.get(inp)
                    if sp is None:
                        options.append(("file", inp.name, inp))
                    elif id(sp) not in seen:
                        seen.add(id(sp))
                        options.append(("parent", f"{sp['orig'].name} [split]", sp))
                return options

            def start_selection():
                options = list_active()
                if not options:
                    transient("  [yellow]Nothing running right now[/yellow]")
                    return
                lines = [f"    {i + 1}) {label}" for i, (_, label, _) in enumerate(options)]
                live_print("  [cyan]Skip which video? (number + Enter, 0/Esc cancels)[/cyan]\n" + "\n".join(lines))
                selecting["options"] = options
                selecting["digits"] = ""
                selecting["expires"] = time.monotonic() + 10

            def cancel_selection(silent=False):
                selecting["options"] = None
                selecting["digits"] = ""
                if not silent:
                    transient("  [dim]Skip cancelled[/dim]")

            def confirm_selection():
                options = selecting["options"] or []
                try:
                    n = int(selecting["digits"] or "0")
                except ValueError:
                    n = 0
                selecting["options"] = None
                selecting["digits"] = ""
                if n < 1 or n > len(options):
                    transient("  [dim]Skip cancelled[/dim]")
                    return
                kind, label, payload = options[n - 1]
                if kind == "file":
                    handle_control(("skip_file", payload))
                    key = str(payload)
                else:
                    handle_control(("skip_parent", payload))
                    key = str(payload["orig"])
                skip_ack["key"] = key
                skip_ack["handle"] = transient(f"  [yellow]Skipping {label}...[/yellow]", linger=None)

            # Live worker ceiling: full CPU count so ] always works upward.
            workers_ceiling = os.cpu_count() or workers

            def adjust_slots(delta):
                slots["max"] = max(1, min(workers_ceiling, slots["max"] + delta))
                # Erased when the new count takes effect (next submit or,
                # when lowering, when running files drain to the new max).
                slots["ack"] = transient(f"  [cyan]Workers: {slots['max']}/{workers_ceiling}[/cyan]", linger=None)

            def handle_control(ctl):
                kind, payload = ctl
                if kind == "skip_file":
                    if compressor.kill_path(payload):
                        user_skipped.add(str(payload))
                    else:
                        transient("  [yellow]That file already finished[/yellow]")
                else:
                    parent = payload
                    killed = False
                    for seg_in, _ in parent["segments"]:
                        if compressor.kill_path(seg_in):
                            killed = True
                        user_skipped.add(str(seg_in))
                    if killed:
                        parent_skipped.add(str(parent["orig"]))
                    else:
                        transient("  [yellow]Those parts already finished[/yellow]")

            def on_key(ch):
                if selecting["options"] is not None:
                    if time.monotonic() > selecting["expires"]:
                        cancel_selection()
                        return
                    if ch in ("\x1b",):
                        cancel_selection()
                    elif ch in ("\r", "\n"):
                        confirm_selection()
                    elif ch.isdigit():
                        if ch == "0" and not selecting["digits"]:
                            cancel_selection()
                        elif len(selecting["digits"]) < 3:
                            selecting["digits"] += ch
                    return
                if not ch:
                    return
                low = ch.lower()
                if low == "p":
                    try:
                        toggle_pause()
                    except Exception:
                        pass
                elif low == "s":
                    try:
                        start_selection()
                    except Exception:
                        pass
                elif ch == "[":
                    adjust_slots(-1)
                elif ch == "]":
                    adjust_slots(+1)

            def pause_listener():
                # Never die on console errors: keep listening until the run
                # ends. Key handling is isolated from compression, so a bad
                # keypress can never break an encode.
                if os.name == "nt":
                    try:
                        import msvcrt
                    except ImportError:
                        return
                    while not stop_flag.is_set():
                        try:
                            if selecting["options"] is not None and time.monotonic() > selecting["expires"]:
                                cancel_selection()
                            if msvcrt.kbhit():
                                try:
                                    ch = msvcrt.getwch()
                                except Exception:
                                    time.sleep(0.1)
                                    continue
                                if ch in ("\x00", "\xe0"):
                                    try:
                                        msvcrt.getwch()
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        on_key(ch)
                                    except Exception:
                                        pass
                            else:
                                time.sleep(0.1)
                        except Exception:
                            time.sleep(0.5)
                else:
                    try:
                        import select
                        import termios
                        import tty
                    except ImportError:
                        return
                    if not sys.stdin.isatty():
                        return
                    fd = sys.stdin.fileno()
                    try:
                        old = termios.tcgetattr(fd)
                    except termios.error:
                        return
                    try:
                        tty.setcbreak(fd)
                        while not stop_flag.is_set():
                            if selecting["options"] is not None and time.monotonic() > selecting["expires"]:
                                cancel_selection()
                            try:
                                r, _, _ = select.select([fd], [], [], 0.1)
                            except Exception:
                                time.sleep(0.5)
                                continue
                            if r:
                                try:
                                    ch = os.read(fd, 1).decode(errors="ignore")
                                except OSError:
                                    time.sleep(0.5)
                                    continue
                                try:
                                    on_key(ch)
                                except Exception:
                                    pass
                    finally:
                        try:
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                        except termios.error:
                            pass

            pause_thread = threading.Thread(target=pause_listener, daemon=True)
            pause_thread.start()

            def splitter():
                for parent in unsplit:
                    if stop_flag.is_set():
                        break
                    while paused.is_set() and not stop_flag.is_set():
                        paused.wait(timeout=0.5)
                    if stop_flag.is_set():
                        break
                    try:
                        parent["parts_dir"].mkdir(parents=True, exist_ok=True)
                        split_file(parent["orig"], parent["parts_dir"],
                                   parent["tag"], parent["cuts"], parent["duration"])
                        events.put(("ready", parent, None))
                    except Exception as e:
                        events.put(("failed", parent, str(e)))

            splitter_thread = threading.Thread(target=splitter, daemon=True)
            splitter_thread.start()

            def handle_event(ev):
                kind, parent, msg = ev
                if kind == "ready":
                    for seg_in, seg_out in parent["segments"]:
                        pending.append((seg_in, seg_out, None))
                else:
                    cleanup_split_parts(parent)
                    for seg_in, _ in parent["segments"]:
                        segment_parents.pop(seg_in, None)
                    result = CompressionResult(
                        input_path=parent["orig"],
                        output_path=parent["out_path"],
                        original_size=parent["original_size"],
                        compressed_size=None,
                                        success=False,
                                        error=f"part {idx + 1}/{total_parts} failed: {err}",
                                    )
                    results.append(result)
                    parent_elapsed = time.monotonic() - parent["start"]
                    live_print(f"  [red][FAIL {count_str()}][/red] {nm(parent['orig'].name)} - {result.error} ({format_time(parent_elapsed)})", highlight=False)

            def submit_one(task):
                input_path, output_path, backup_path = task
                settle_ui()
                slots.pop("ack", None)
                try:
                    size = input_path.stat().st_size
                except OSError:
                    size = 0
                if size <= 0:
                    size = 1
                # Create per-file progress task only when the file starts.
                # Bars count bytes (not percent) so speed/ETA columns are real.
                seg_parent = segment_parents.get(input_path)
                if seg_parent is not None:
                    idx = next((i for i, (s, _) in enumerate(seg_parent["segments"])
                               if s == input_path), 0)
                    label = f"{seg_parent['orig'].stem} [{idx + 1}/{len(seg_parent['segments'])}]"
                else:
                    label = input_path.name
                skips = compressor.plan_skips(input_path)
                if skips:
                    label += f" (skip {','.join(skips)})"
                file_task = progress.add_task(f"[cyan]{label}", total=size)
                file_tasks[input_path] = file_task
                file_sizes[input_path] = size
                file_reported[input_path] = 0
                file_start_times[input_path] = time.monotonic()
                progress.refresh()

                def make_callback(task_id, path, total):
                    def callback(pct):
                        completed = int(total * pct / 100)
                        if completed < 0:
                            completed = 0
                        elif completed > total:
                            completed = total
                        with progress_lock:
                            last = file_reported.get(path, 0)
                            if completed > last:
                                file_reported[path] = completed
                                progress.update(task_id, completed=completed)
                                progress.advance(overall_task, completed - last)
                    return callback

                future = executor.submit(
                    compressor.compress,
                    input_path,
                    output_path,
                    False,
                    make_callback(file_task, input_path, size),
                    force=input_path in segment_parents,
                )
                future_to_task[future] = (input_path, output_path, backup_path, file_task)

            min_free_gb = config["processing"].get("min_free_space_gb") or 0
            low_space = {"hit": False}

            def drive_free_gb(path) -> Optional[float]:
                try:
                    p = Path(path)
                    probe = str(p.parent if p.parent.exists() else p.anchor)
                    return shutil.disk_usage(probe).free / (1024 ** 3)
                except OSError:
                    return None

            def submit_ready():
                while paused.is_set() and not stop_flag.is_set():
                    paused.wait(timeout=0.5)
                while pending and len(future_to_task) < slots["max"]:
                    if min_free_gb > 0 and not low_space["hit"]:
                        free = drive_free_gb(pending[0][1])
                        if free is not None and free < min_free_gb:
                            low_space["hit"] = True
                            live_print(
                                f"  [red]Low disk space ({free:.1f} GB free < {min_free_gb:.1f} GB): "
                                "no new files queued. Active encodes finish.[/red]")
                            skipped_bytes = 0
                            done_parents = set()
                            while pending:
                                f, _, _ = pending.pop(0)
                                parent = segment_parents.get(f)
                                if parent is not None:
                                    key = str(parent["orig"])
                                    if key not in done_parents:
                                        done_parents.add(key)
                                        parent_skipped.add(key)
                                        skipped_bytes += parent["original_size"]
                                        for seg_in, _ in parent["segments"]:
                                            if compressor.kill_path(seg_in):
                                                user_skipped.add(str(seg_in))
                                    continue
                                try:
                                    size = f.stat().st_size
                                except OSError:
                                    size = 0
                                skipped_bytes += size
                                results.append(CompressionResult(
                                    input_path=f, output_path=None,
                                    original_size=size, compressed_size=None,
                                    success=True, skipped=True,
                                    skip_reason="low disk space",
                                ))
                            if skipped_bytes:
                                progress.update(overall_task, total=max(1, overall_total - skipped_bytes))
                            break
                    submit_one(pending.pop(0))

            # Process results as they complete, starting the next pending file
            try:
                while future_to_task or splitter_thread.is_alive() or pending or not events.empty():
                    while True:
                        try:
                            handle_event(events.get_nowait())
                        except queue.Empty:
                            break
                    submit_ready()
                    if not future_to_task:
                        splitter_thread.join(timeout=0.5)
                        continue
                    done, _ = wait(list(future_to_task), timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        input_path, output_path, backup_path, file_task = future_to_task.pop(future)
                        # Lowered worker count just took effect: drop the ack
                        # (its line is settled with everything else below).
                        if "ack" in slots and len(future_to_task) <= slots["max"]:
                            slots.pop("ack", None)
                        try:
                            result = future.result()
                        except Exception as e:
                            result = CompressionResult(
                                input_path=input_path,
                                output_path=output_path,
                                original_size=input_path.stat().st_size if input_path.exists() else 0,
                                compressed_size=None,
                                success=False,
                                error=str(e),
                            )

                        if str(input_path) in user_skipped:
                            # Killed on user request: convert to a skip, drop partial output
                            user_skipped.discard(str(input_path))
                            if output_path != input_path:
                                try:
                                    output_path.unlink(missing_ok=True)
                                except OSError:
                                    pass
                            try:
                                original_size = input_path.stat().st_size
                            except OSError:
                                original_size = 0
                            result = CompressionResult(
                                input_path=input_path, output_path=None,
                                original_size=original_size, compressed_size=None,
                                success=True, skipped=True,
                                skip_reason="skipped by user",
                            )
                            fire_skip_ack(str(input_path))

                        seg_parent = segment_parents.get(input_path)
                        if seg_parent is None:
                            # Finalize file handling
                            result = file_handler.finalize(result, backup_path, input_path)
                            results.append(result)
                            if result.success and not result.skipped:
                                done_log.mark(input_path, current_settings)
                        else:
                            seg_parent["seg_results"].append(result)

                        # Update progress - mark file task complete, advance overall
                        end = time.monotonic()
                        start = file_start_times.pop(input_path, end)
                        file_elapsed = max(0.0, end - start - paused_overlap(start, end))
                        size = file_sizes.pop(input_path, 0)
                        with progress_lock:
                            last = file_reported.pop(input_path, 0)
                            if size > last:
                                progress.update(file_task, completed=size)
                                progress.advance(overall_task, size - last)
                        settle_ui()
                        progress.remove_task(file_task)
                        progress.refresh()

                        # Print individual result
                        if seg_parent is None:
                            if result.skipped:
                                live_print(f"  [yellow][SKIP {count_str()}][/yellow] {nm(input_path.name)} - [dim]skipped: {result.skip_reason}[/dim]", highlight=False)
                            elif result.success:
                                savings = result.original_size - (result.compressed_size or 0)
                                pct = (savings / result.original_size) * 100 if result.original_size > 0 else 0
                                crf_note = f", [magenta]CRF {result.crf}[/magenta]" if result.crf is not None else ""
                                skip_note = (f", [yellow]skipped {','.join(result.skipped_methods)}[/yellow]"
                                             if result.skipped_methods else "")
                                live_print(f"  [green][OK {count_str()}][/green] {nm(input_path.name)} - [yellow]{format_size(result.original_size)} -> {format_size(result.compressed_size or 0)}[/yellow] ([green]{pct:.1f}% saved[/green], {format_time(file_elapsed)}{crf_note}{skip_note})", highlight=False)
                            else:
                                live_print(f"  [red][FAIL {count_str()}][/red] {nm(input_path.name)} - {result.error} ({format_time(file_elapsed)})", highlight=False)
                        else:
                            idx = next((i for i, (s, _) in enumerate(seg_parent["segments"]) if s == input_path), 0)
                            total_parts = len(seg_parent["segments"])
                            if result.skipped:
                                pass
                            elif verbose and result.success:
                                psaved = result.original_size - (result.compressed_size or 0)
                                ppct = (psaved / result.original_size) * 100 if result.original_size > 0 else 0
                                live_print(f"  [dim]part {idx + 1}/{total_parts} of {seg_parent['orig'].name} done - {format_size(result.original_size)} -> {format_size(result.compressed_size or 0)} ({ppct:.1f}%, {format_time(file_elapsed)})[/dim]", highlight=False)
                            elif verbose:
                                live_print(f"  [dim]part {idx + 1}/{total_parts} of {seg_parent['orig'].name} done[/dim]", highlight=False)
                            if len(seg_parent["seg_results"]) >= total_parts:
                                parent_end = time.monotonic()
                                parent_elapsed = max(0.0, parent_end - seg_parent["start"] - paused_overlap(seg_parent["start"], parent_end))
                                if str(seg_parent["orig"]) in parent_skipped:
                                    parent_skipped.discard(str(seg_parent["orig"]))
                                    cleanup_split_parts(seg_parent)
                                    for seg_in, _ in seg_parent["segments"]:
                                        segment_parents.pop(seg_in, None)
                                    final = CompressionResult(
                                        input_path=seg_parent["orig"], output_path=None,
                                        original_size=seg_parent["original_size"],
                                        compressed_size=None, success=True, skipped=True,
                                        skip_reason="skipped by user",
                                    )
                                    fire_skip_ack(str(seg_parent["orig"]))
                                    results.append(final)
                                    live_print(f"  [yellow][SKIP {count_str()}][/yellow] {nm(seg_parent['orig'].name)} - [dim]skipped by user[/dim]", highlight=False)
                                    continue
                                # A failed part can never join: fail fast with
                                # the part's real error instead of a doomed
                                # concat on its broken leftover.
                                part_failed = next((r for r in seg_parent["seg_results"]
                                                    if not r.success and not r.skipped), None)
                                if part_failed is not None:
                                    idx = next((i for i, (s, _) in enumerate(seg_parent["segments"])
                                                if s == part_failed.input_path), -1)
                                    err = (part_failed.error or "unknown error")[-300:]
                                    if verbose and part_failed.error:
                                        live_print(f"  [dim]part {idx + 1}/{total_parts} error: {part_failed.error[-1500:]}[/dim]", highlight=False)
                                    cleanup_split_parts(seg_parent)
                                    for seg_in, _ in seg_parent["segments"]:
                                        segment_parents.pop(seg_in, None)
                                    final = CompressionResult(
                                        input_path=seg_parent["orig"],
                                        output_path=seg_parent["out_path"],
                                        original_size=seg_parent["original_size"],
                                        compressed_size=None, success=False,
                                        error=f"part failed: {err}",
                                    )
                                    results.append(final)
                                    live_print(f"  [red][FAIL {count_str()}][/red] {nm(seg_parent['orig'].name)} - {final.error} ({format_time(parent_elapsed)})", highlight=False)
                                    continue
                                final = finish_split_parent(seg_parent, file_handler)
                                results.append(final)
                                if final.success and not final.skipped:
                                    done_log.mark(seg_parent["orig"], current_settings)
                                for seg_in, _ in seg_parent["segments"]:
                                    segment_parents.pop(seg_in, None)
                                if final.success:
                                    savings = final.original_size - (final.compressed_size or 0)
                                    pct = (savings / final.original_size) * 100 if final.original_size > 0 else 0
                                    skip_note = (f", skipped {','.join(final.skipped_methods)}"
                                                 if final.skipped_methods else "")
                                    live_print(f"  [green][OK {count_str()}][/green] {nm(seg_parent['orig'].name)} - [yellow]{format_size(final.original_size)} -> {format_size(final.compressed_size or 0)}[/yellow] ([green]{pct:.1f}% saved[/green], {format_time(parent_elapsed)}, [dim]{total_parts} parts{skip_note}[/dim])", highlight=False)
                                else:
                                    live_print(f"  [red][FAIL {count_str()}][/red] {nm(seg_parent['orig'].name)} - {final.error} ({format_time(parent_elapsed)})", highlight=False)
            except KeyboardInterrupt:
                # Acknowledge first, then stop fast: kill running FFmpeg
                # jobs, drop the pending queue.
                live_print("  [red]Quitting...[/red]")
                stop_flag.set()
                compressor.terminate_all()
                # Delete unfinished work only: split parts of every open
                # parent, plus partial whole-file outputs. Originals are
                # never candidates (overwrite outputs equal their input
                # and are skipped via normcase comparison).
                # Every unfinished parent, not just ones with segments in
                # flight: the splitter cuts ahead, so planned-but-unstarted
                # parents also have part files on disk. Finished parents
                # already popped their segments from segment_parents.
                seen_parents = set()
                for sp in list(segment_parents.values()):
                    if id(sp) not in seen_parents:
                        seen_parents.add(id(sp))
                        cleanup_split_parts(sp)
                for _fut, (inp, outp, _b, _t) in list(future_to_task.items()):
                    if segment_parents.get(inp) is not None:
                        continue
                    try:
                        same = (os.path.normcase(os.path.abspath(outp))
                                == os.path.normcase(os.path.abspath(inp)))
                    except OSError:
                        same = False
                    if not same:
                        try:
                            outp.unlink(missing_ok=True)
                        except OSError:
                            pass
                # Same sweep as --clean (no prompt): temp/test/sample
                # encodes, concat lists, and .compressor-parts contents.
                try:
                    for p in collect_staging(args.input):
                        try:
                            p.unlink(missing_ok=True)
                        except OSError:
                            pass
                    roots = [args.input] if args.input.is_dir() else [args.input.parent]
                    for root in roots:
                        try:
                            dirs = list(root.rglob(PARTS_DIR_NAME))
                        except OSError:
                            continue
                        for d in dirs:
                            try:
                                d.rmdir()
                            except OSError:
                                pass
                except Exception:
                    pass
                executor.shutdown(cancel_futures=True)
                raise

    # Print summary
    ui_state["live"] = False
    end = time.monotonic()
    print_summary(results, total_elapsed=max(0.0, end - run_start - paused_overlap(run_start, end)))

    # Analyze after actual compression if requested
    if args.analyze:
        run_analysis(files, config, args, actual_results=results)

    # Exit code
    failed = sum(1 for r in results if not r.success and not r.skipped)
    return 1 if failed > 0 else 0


def main():
    """Entry point."""
    parser = create_parser()
    args = parser.parse_args()

    # Validate input
    if not args.input.exists():
        console.print(f"[red]Error: Input path does not exist: {args.input}[/red]")
        return 1

    try:
        return run_compression(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        return 130
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())