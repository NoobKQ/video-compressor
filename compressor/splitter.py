"""Split big videos into keyframe-aligned segments for parallel encoding."""

import subprocess
from pathlib import Path
from typing import List, Optional

# Shared staging dir (per input folder) for segment files. Always excluded
# from scans so interrupted runs never reprocess leftovers.
PARTS_DIR_NAME = ".compressor-parts"


def _run(cmd: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def get_duration(input_path: Path) -> Optional[float]:
    """Probe container duration in seconds."""
    try:
        r = _run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ])
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        pass
    return None


def get_keyframes(input_path: Path) -> List[float]:
    """Return sorted keyframe timestamps in seconds.

    Packet flags mark keyframes with K (some ffprobe builds ignore
    -skip_frame, so filter client-side; still packet-level, no decoding).
    """
    try:
        r = _run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0",
            str(input_path),
        ])
        if r.returncode != 0:
            return []
        keys = []
        for line in r.stdout.splitlines():
            parts = line.strip().split(",")
            if len(parts) != 2:
                continue
            pts, flags = parts
            if not flags.startswith("K"):
                continue
            try:
                keys.append(float(pts))
            except ValueError:
                continue
        return sorted(keys)
    except (subprocess.SubprocessError, OSError):
        return []


def pick_cuts(duration: float, keyframes: List[float], parts: int) -> List[float]:
    """Pick keyframe-aligned cut points for `parts` segments."""
    if parts < 2 or not duration or duration <= 0:
        return []
    cuts = []
    for i in range(1, parts):
        target = duration * i / parts
        after = [k for k in keyframes if k >= target]
        before = [k for k in keyframes if k < target]
        candidates = []
        if after:
            candidates.append(after[0])
        if before:
            candidates.append(before[-1])
        if not candidates:
            continue
        chosen = min(candidates, key=lambda k: abs(k - target))
        if chosen <= 0 or chosen >= duration:
            continue
        if cuts and chosen - cuts[-1] < 1.0:
            continue
        cuts.append(chosen)
    return cuts


def split_file(input_path: Path, parts_dir: Path, tag: str,
               cuts: List[float], duration: float) -> List[Path]:
    """Cut the file into keyframe-aligned segments (stream copy, no re-encode)."""
    bounds = [0.0] + cuts + [duration]
    segments = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        seg = parts_dir / f"{tag}_src{i:02d}{input_path.suffix}"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", str(input_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(seg),
        ]
        r = _run(cmd, timeout=1800)
        if r.returncode != 0 or not seg.exists():
            raise RuntimeError(f"split failed at {start:.1f}s: {r.stderr[-500:]}")
        segments.append(seg)
    return segments


def concat_files(parts: List[Path], output: Path, tag: str) -> None:
    """Join encoded segments losslessly (same codec/params, stream copy)."""
    list_file = output.parent / f"{tag}.concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.resolve().as_posix()}'\n")
    try:
        # Joining rewrites the whole file (faststart second pass), so allow
        # plenty of time for multi-GB outputs.
        r = _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ], timeout=3600)
        if r.returncode != 0 or not output.exists():
            raise RuntimeError(f"concat failed: {r.stderr[-500:]}")
    finally:
        try:
            list_file.unlink(missing_ok=True)
        except OSError:
            pass
