"""Core compression logic using FFmpeg."""

import subprocess
import hashlib
import json
import os
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional
from compressor.gpu import detect_gpu, get_best_encoder, check_hardware_encoder, GPUVendor


@dataclass
class CompressionResult:
    """Result of a compression operation."""
    input_path: Path
    output_path: Optional[Path]
    original_size: int
    compressed_size: Optional[int]
    success: bool
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    estimated_time: Optional[float] = None  # seconds
    crf: Optional[int] = None  # actual CRF/CQ used (differs from config with --max-size)
    skipped_methods: Optional[List[str]] = None  # requested but not applied: scale/fps/audio/video


def _parse_bitrate(value) -> Optional[int]:
    """Parse a bitrate like '128k', '1.5M', or 128000 into bits per second."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except ValueError:
        return None


def ffprobe_json(input_path: Path, timeout: int = 60) -> Optional[dict]:
    """Run ffprobe with a timeout; None on any failure (never hangs)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_format", "-show_streams", "-of", "json",
             str(input_path)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        return data if isinstance(data, dict) else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _win_suspend_threads(pid: int, suspend: bool) -> bool:
    """Suspend/resume a whole process in one syscall (Windows)."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # NtSuspendProcess/NtResumeProcess beat the old thread-by-thread walk,
    # which snapshotted every thread on the system per process and took
    # seconds with several encodes running.
    PROCESS_SUSPEND_RESUME = 0x0800
    try:
        ntdll = ctypes.windll.ntdll
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            return False
        try:
            fn = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
            return fn(handle) == 0  # STATUS_SUCCESS
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


class VideoCompressor:
    """Handles video compression using FFmpeg."""

    def __init__(self, config: dict):
        self.config = config
        self.encoding = config.get("encoding", {})
        self.processing = config.get("processing", {})
        self.gpu_config = config.get("gpu", {})
        self.gpu_info = None
        if self.gpu_config.get("enabled", False):
            self.gpu_info = detect_gpu()
        self._active_processes = set()
        self._proc_for_path = {}
        self._suspended = set()
        self._pause_enforced = False
        self._proc_lock = threading.Lock()

    def kill_path(self, input_path) -> bool:
        """Kill the running encode for one path (user skip)."""
        with self._proc_lock:
            proc = self._proc_for_path.get(str(input_path))
        if proc is None:
            return False
        try:
            if proc.poll() is None:
                proc.kill()
                return True
        except Exception:
            pass
        return False

    def terminate_all(self):
        """Kill all currently running FFmpeg processes (fast stop on Ctrl+C)."""
        with self._proc_lock:
            procs = list(self._active_processes)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    @staticmethod
    def _suspend_sys(proc, suspend: bool) -> bool:
        """Suspend/resume one process. Returns True on success."""
        try:
            if proc.poll() is not None:
                return False
            if os.name == "nt":
                return _win_suspend_threads(proc.pid, suspend)
            import signal
            os.kill(proc.pid, signal.SIGSTOP if suspend else signal.SIGCONT)
            return True
        except Exception:
            return False

    def _set_suspended(self, proc, suspend: bool) -> None:
        with self._proc_lock:
            if suspend:
                if proc in self._suspended or not self._suspend_sys(proc, True):
                    return
                self._suspended.add(proc)
            else:
                if proc not in self._suspended:
                    return
                self._suspend_sys(proc, False)
                self._suspended.discard(proc)

    def pause_mark(self, paused: bool) -> None:
        """Flip the enforce flag synchronously; bulk suspend/resume runs async."""
        with self._proc_lock:
            self._pause_enforced = paused

    def suspend_all(self):
        """Suspend all running FFmpeg processes (pause). New ones pause too."""
        with self._proc_lock:
            self._pause_enforced = True
            targets = [p for p in self._active_processes if p not in self._suspended]
        for p in targets:
            self._set_suspended(p, True)

    def resume_all(self):
        """Resume all suspended FFmpeg processes."""
        with self._proc_lock:
            self._pause_enforced = False
            targets = list(self._suspended)
        for p in targets:
            self._set_suspended(p, False)

    def get_video_info(self, input_path: Path) -> dict:
        """Get video information using ffprobe."""
        try:
            probe = ffprobe_json(input_path)
            if probe is None:
                return {"error": "ffprobe failed or timed out"}
            video_stream = next(
                (s for s in probe["streams"] if s["codec_type"] == "video"), None
            )
            audio_stream = next(
                (s for s in probe["streams"] if s["codec_type"] == "audio"), None
            )

            video_fps = None
            if video_stream:
                for key in ("avg_frame_rate", "r_frame_rate"):
                    rate = video_stream.get(key)
                    if rate and rate not in ("0/0", "0"):
                        try:
                            num, den = rate.split("/")
                            den = float(den)
                            if den > 0:
                                video_fps = float(num) / den
                                break
                        except (ValueError, ZeroDivisionError):
                            continue

            audio_bitrate = None
            if audio_stream and audio_stream.get("bit_rate"):
                try:
                    audio_bitrate = int(float(audio_stream["bit_rate"]))
                except (ValueError, TypeError):
                    audio_bitrate = None

            return {
                "duration": float(probe["format"].get("duration", 0)),
                "size": int(probe["format"].get("size", 0)),
                "bitrate": int(probe["format"].get("bit_rate", 0)),
                "video_codec": video_stream.get("codec_name") if video_stream else None,
                "video_width": video_stream.get("width") if video_stream else None,
                "video_height": video_stream.get("height") if video_stream else None,
                "fps": video_fps,
                "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
                "audio_bitrate": audio_bitrate,
            }
        except Exception as e:
            return {"error": str(e)}

    def is_already_compressed(self, input_path: Path) -> tuple[bool, str]:
        """Check if file appears to already be compressed."""
        info = self.get_video_info(input_path)
        if "error" in info:
            return False, f"Could not probe: {info['error']}"

        # Heuristics for detecting already compressed files
        video_codec = info.get("video_codec", "").lower()
        size_mb = info.get("size", 0) / (1024 * 1024)
        min_size = self.processing.get("min_size_mb", 1)

        if size_mb < min_size:
            return True, f"File too small ({size_mb:.1f}MB < {min_size}MB)"

        # Already using efficient codecs
        efficient_codecs = ["hevc", "h265", "vp9", "av1"]
        if video_codec in efficient_codecs:
            return True, f"Already using efficient codec: {video_codec}"

        # High compression already (low bitrate for resolution)
        width = info.get("video_width", 0)
        height = info.get("video_height", 0)
        bitrate = info.get("bitrate", 0)

        if width and height and bitrate:
            pixels = width * height
            bits_per_pixel = bitrate / pixels if pixels > 0 else 0
            # Very rough heuristic: < 0.1 bits/pixel is heavily compressed
            if bits_per_pixel < 0.05:
                return True, f"Already heavily compressed ({bits_per_pixel:.3f} bits/pixel)"

        return False, ""

    def build_output_path(self, input_path: Path, mode: str, output_dir: Optional[Path] = None) -> Path:
        """Build output path based on file handling mode."""
        stem = input_path.stem
        suffix = ".mp4"  # Standardize output to mp4

        if mode == "overwrite":
            return input_path.with_suffix(suffix)
        elif mode == "backup":
            return input_path.with_suffix(suffix)
        else:  # keep
            if output_dir:
                return output_dir / f"{stem}{suffix}"
            return input_path.parent / "compressed" / f"{stem}{suffix}"

    def compress(self, input_path: Path, output_path: Path, dry_run: bool = False, progress_callback=None, force: bool = False) -> CompressionResult:
        """Compress a single video file with optional progress callback.

        force=True bypasses the already-compressed skip (used for segments).
        """
        original_size = input_path.stat().st_size

        # Check if already compressed
        if self.processing.get("skip_compressed", True) and not force:
            skipped, reason = self.is_already_compressed(input_path)
            if skipped:
                return CompressionResult(
                    input_path=input_path,
                    output_path=None,
                    original_size=original_size,
                    compressed_size=None,
                    success=True,
                    skipped=True,
                    skip_reason=reason,
                )

        if dry_run:
            # Do a quick test encode (first 5 seconds) for accurate estimate
            import time
            start_time = time.time()
            try:
                vcodec, preset, crf, extra_args = self._get_encoder_settings()

                info = self.get_video_info(input_path)
                acodec, ab = self._get_audio_settings(info)
                video_filters = self._build_video_filters(info)
                
                test_output = output_path.with_suffix(".test.mp4")
                
                cmd = [
                    "ffmpeg", "-y",
                    "-i", str(input_path),
                    "-t", "5",  # Only first 5 seconds
                    "-c:v", vcodec,
                    *extra_args,
                ]
                if video_filters:
                    cmd.extend(["-vf", video_filters])
                audio_filter = self._get_audio_filter(acodec)
                if audio_filter:
                    cmd.extend(["-af", audio_filter])
                cmd.extend(["-c:a", acodec])
                if acodec != "copy" and ab:
                    cmd.extend(["-b:a", ab])
                cmd.extend([
                    "-movflags", "+faststart",
                    "-map_metadata", "-1",
                    str(test_output),
                ])
                
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                
                elapsed = time.time() - start_time
                
                if result.returncode == 0 and test_output.exists():
                    test_size = test_output.stat().st_size
                    # Get duration to extrapolate
                    duration = info.get("duration", 0)
                    if duration > 5:
                        estimated_size = int(test_size * (duration / 5))
                        estimated_time = elapsed * (duration / 5)
                    else:
                        estimated_size = test_size
                        estimated_time = elapsed
                    test_output.unlink()
                else:
                    estimated_size = int(original_size * 0.5)
                    estimated_time = None
            except Exception:
                estimated_size = int(original_size * 0.5)
                estimated_time = None
            
            return CompressionResult(
                input_path=input_path,
                output_path=output_path,
                original_size=original_size,
                compressed_size=estimated_size,
                success=True,
                estimated_time=estimated_time,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Get duration for progress calculation
            info = self.get_video_info(input_path)
            duration = info.get("duration", 0)
            acodec, ab = self._get_audio_settings(info)

            # Build video filters
            video_filters = self._build_video_filters(info)

            # --max-size: search a CRF that lands at/under the target
            # (whole files only; split segments use the base CRF).
            max_mb = self.encoding.get("max_size_mb")
            crf_override = None
            if max_mb and not force and duration > 0:
                target_bytes = int(float(max_mb) * 1024 * 1024)
                if original_size > target_bytes:
                    base_vcodec, base_preset, base_crf, _ = self._get_encoder_settings()
                    sample_path = output_path.with_name(output_path.stem + ".sizetest.mp4")
                    crf_override = self._find_crf_for_size(
                        input_path, sample_path, info, duration,
                        base_vcodec, base_preset, base_crf,
                        acodec, ab, video_filters, target_bytes)

            # Determine encoder settings (GPU or CPU)
            vcodec, preset, crf, extra_args = self._get_encoder_settings(crf_override)
            threads = self.encoding.get("threads", 0)
            skipped_methods = self.describe_skips(info, video_filters, acodec, vcodec)

            # Build FFmpeg command with progress output
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-c:v", vcodec,
                *extra_args,
            ]
            if video_filters:
                cmd.extend(["-vf", video_filters])
            audio_filter = self._get_audio_filter(acodec)
            if audio_filter:
                cmd.extend(["-af", audio_filter])
            cmd.extend(["-c:a", acodec])
            if acodec != "copy" and ab:
                cmd.extend(["-b:a", ab])
            cmd.extend([
                "-movflags", "+faststart",
                "-map_metadata", "-1",
                "-progress", "pipe:1",
                "-nostats",
                str(output_path),
            ])
            if threads > 0:
                cmd.insert(-1, "-threads")
                cmd.insert(-1, str(threads))

            # Run with real-time progress parsing.
            # UTF-8 with replacement: filenames with emoji/non-latin chars
            # appear in ffmpeg's stderr and must not crash decoding.
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._proc_lock:
                self._active_processes.add(process)
                self._proc_for_path[str(input_path)] = process
                enforce = self._pause_enforced
            if enforce:
                self._set_suspended(process, True)

            try:
                current_time = 0.0
                stderr_lines = []

                def read_stderr():
                    for line in process.stderr:
                        stderr_lines.append(line)

                stderr_thread = threading.Thread(target=read_stderr, daemon=True)
                stderr_thread.start()

                for line in process.stdout:
                    line = line.strip()
                    if line.startswith("out_time_ms="):
                        try:
                            current_time = int(line.split("=")[1]) / 1_000_000
                            if progress_callback and duration > 0:
                                progress_callback(current_time / duration * 100)
                        except (ValueError, IndexError):
                            pass
                    elif line == "progress=end":
                        break

                process.wait()
                stderr_thread.join(timeout=2)
                stderr_output = "".join(stderr_lines)
            finally:
                with self._proc_lock:
                    self._active_processes.discard(process)
                    self._suspended.discard(process)
                    self._proc_for_path.pop(str(input_path), None)

            if process.returncode != 0:
                raise subprocess.CalledProcessError(
                    process.returncode, cmd, output=None, stderr=stderr_output)

            compressed_size = output_path.stat().st_size
            if progress_callback:
                progress_callback(100)
            return CompressionResult(
                input_path=input_path,
                output_path=output_path,
                original_size=original_size,
                compressed_size=compressed_size,
                success=True,
                crf=crf if crf_override is not None else None,
                skipped_methods=skipped_methods or None,
            )

        except subprocess.CalledProcessError as e:
            # ffmpeg banners are dozens of lines; the cause is at the end.
            raw = e.stderr if e.stderr else str(e)
            error_msg = "\n".join(raw.strip().splitlines()[-8:])
            return CompressionResult(
                input_path=input_path,
                output_path=output_path,
                original_size=original_size,
                compressed_size=None,
                success=False,
                error=error_msg,
            )
        except Exception as e:
            return CompressionResult(
                input_path=input_path,
                output_path=output_path,
                original_size=original_size,
                compressed_size=None,
                success=False,
                error=str(e),
            )

    def _find_crf_for_size(self, input_path: Path, sample_path: Path, info: dict,
                           duration: float, vcodec: str, preset: str, base_crf: int,
                           acodec: str, ab, video_filters, target_bytes: int) -> int:
        """Search a CRF whose full-file output lands at/under target_bytes.

        Encodes a short middle sample at up to 3 CRF values and extrapolates.
        Uses the x264 rule of thumb (CRF +/-6 ~= half/double bitrate);
        approximate for GPU quality scales. Never returns below base_crf.
        """
        import math
        sample_dur = min(20.0, max(5.0, duration * 0.05))
        if duration <= sample_dur + 1:
            return base_crf
        start = max(0.0, duration / 2 - sample_dur / 2)
        crf = base_crf
        for _ in range(3):
            _, _, _, extra_args = self._get_encoder_settings(crf)
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start:.2f}",
                "-i", str(input_path),
                "-t", f"{sample_dur:.2f}",
                "-c:v", vcodec,
                *extra_args,
            ]
            if video_filters:
                cmd.extend(["-vf", video_filters])
            audio_filter = self._get_audio_filter(acodec)
            if audio_filter:
                cmd.extend(["-af", audio_filter])
            cmd.extend(["-c:a", acodec])
            if acodec != "copy" and ab:
                cmd.extend(["-b:a", ab])
            cmd.extend([
                "-movflags", "+faststart",
                "-map_metadata", "-1",
                str(sample_path),
            ])
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=120)
                if r.returncode != 0 or not sample_path.exists():
                    break
                sample_size = sample_path.stat().st_size
                estimated = sample_size * (duration / sample_dur)
                if estimated <= target_bytes:
                    break
                crf = min(51, crf + max(1, int(round(6 * math.log2(estimated / target_bytes)))))
            except (subprocess.SubprocessError, OSError, ValueError):
                break
            finally:
                try:
                    sample_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return max(base_crf, min(crf, 51))

    def _scale_target_dims(self, scale, width: int, height: int) -> Optional[tuple]:
        """Resolve a scale value to (width, height), adjusted to even numbers."""
        if isinstance(scale, str):
            # Named resolution (1080p, 720p, etc.) or WIDTHxHEIGHT
            scale_lower = scale.lower()
            if scale_lower.endswith("p"):
                try:
                    target_height = int(scale_lower[:-1])
                except ValueError:
                    return None
                target_width = int(width * target_height / height)
            elif "x" in scale_lower:
                try:
                    target_width, target_height = map(int, scale_lower.split("x"))
                except ValueError:
                    return None
            else:
                # Assume scale factor (0.5 = 50%)
                try:
                    factor = float(scale)
                except ValueError:
                    return None
                target_width = int(width * factor)
                target_height = int(height * factor)
        elif isinstance(scale, (int, float)):
            # Numeric scale factor
            factor = float(scale)
            target_width = int(width * factor)
            target_height = int(height * factor)
        else:
            return None

        # Ensure even dimensions (required by most codecs)
        target_width = target_width + (target_width % 2)
        target_height = target_height + (target_height % 2)
        return target_width, target_height

    def _get_audio_filter(self, acodec: str) -> Optional[str]:
        """Audio filter for the encode, if the codec needs one.

        libopus only accepts 8/12/16/24/48 kHz; resample anything else
        (e.g. 44.1 kHz sources fail with 'Invalid argument' otherwise).
        A no-op when the source is already 48 kHz.
        """
        if acodec == "libopus":
            return "aresample=48000"
        return None

    def _get_audio_settings(self, info: dict) -> tuple:
        """Return (codec, bitrate); falls back to copy when source is below threshold."""
        acodec = self.encoding.get("audio_codec", "aac")
        ab = self.encoding.get("audio_bitrate", "128k")
        if acodec != "copy":
            threshold = self.encoding.get("audio_threshold") or ab
            source_ab = info.get("audio_bitrate")
            target_ab = _parse_bitrate(threshold)
            if source_ab is not None and target_ab is not None and source_ab <= target_ab:
                return "copy", None
        return acodec, ab

    def plan_skips(self, input_path: Path) -> List[str]:
        """Skipped methods for a file, for progress labels. Never raises."""
        try:
            info = self.get_video_info(input_path)
            if "error" in info:
                return []
            video_filters = self._build_video_filters(info)
            acodec, _ = self._get_audio_settings(info)
            vcodec, _, _, _ = self._get_encoder_settings()
            return self.describe_skips(info, video_filters, acodec, vcodec)
        except Exception:
            return []

    def describe_skips(self, info: dict, video_filters, acodec: str, vcodec: str) -> List[str]:
        """Name requested methods that were not applied (threshold skips).

        Derived from the built filters/settings, so it can never drift from
        the real threshold logic.
        """
        skips = []
        video_config = self.config.get("video", {})
        if video_config.get("scale") and (not video_filters or "scale=" not in video_filters):
            skips.append("scale")
        if video_config.get("fps"):
            try:
                fps_f = float(video_config["fps"])
            except (TypeError, ValueError):
                fps_f = None
            if (not video_filters or "fps=" not in video_filters) and fps_f:
                skips.append("fps")
        if acodec == "copy" and self.encoding.get("audio_codec", "aac") != "copy":
            skips.append("audio")
        if vcodec == "copy" and self.encoding.get("codec", "libx264") == "copy":
            skips.append("video")
        return skips

    def _build_video_filters(self, info: dict) -> Optional[str]:
        """Build video filter chain, skipping processes below their thresholds."""
        filters = []
        video_config = self.config.get("video", {})
        width = info.get("video_width")
        height = info.get("video_height")

        # Scale filter (skipped when source is below the threshold)
        scale = video_config.get("scale")
        if scale and width and height:
            target = self._scale_target_dims(scale, width, height)
            threshold = video_config.get("scale_threshold") or scale
            threshold_dims = self._scale_target_dims(threshold, width, height)
            threshold_h = threshold_dims[1] if threshold_dims else None
            if target is not None and (threshold_h is None or height >= threshold_h):
                keep_aspect = video_config.get("keep_aspect", True)
                if keep_aspect:
                    filters.append(f"scale={target[0]}:{target[1]}:force_original_aspect_ratio=decrease")
                else:
                    filters.append(f"scale={target[0]}:{target[1]}")
                # force_original_aspect_ratio can re-derive odd dims (e.g.
                # 1458x819) that codecs reject; force even as the last step.
                filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

        # FPS filter (skipped when source is below the threshold)
        fps = video_config.get("fps")
        if fps:
            threshold = video_config.get("fps_threshold")
            if threshold is None:
                threshold = fps
            try:
                threshold_f = float(threshold)
            except (TypeError, ValueError):
                threshold_f = None
            source_fps = info.get("fps")
            if source_fps is None or threshold_f is None or source_fps >= threshold_f:
                filters.append(f"fps={fps}")

        return ",".join(filters) if filters else None

    def _get_encoder_settings(self, crf_override=None):
        """Get encoder settings based on GPU/CPU configuration."""
        crf = self.encoding.get("crf", 23) if crf_override is None else crf_override
        # CPU encoding (default)
        if not self.gpu_config.get("enabled", False) or not self.gpu_info or self.gpu_info.vendor == GPUVendor.NONE:
            return (
                self.encoding.get("codec", "libx264"),
                self.encoding.get("preset", "medium"),
                crf,
                ["-preset", self.encoding.get("preset", "medium"), "-crf", str(crf)]
            )

        # GPU encoding
        codec = self.encoding.get("codec", "libx264").replace("libx264", "h264").replace("libx265", "hevc")
        hw_encoder = get_best_encoder(self.gpu_info, codec)

        if not hw_encoder or not check_hardware_encoder(hw_encoder):
            # Fallback to CPU
            return (
                self.encoding.get("codec", "libx264"),
                self.encoding.get("preset", "medium"),
                crf,
                ["-preset", self.encoding.get("preset", "medium"), "-crf", str(crf)]
            )

        # Map preset for hardware encoders
        cpu_preset = self.encoding.get("preset", "medium")
        gpu_preset_map = self._map_preset_to_gpu(cpu_preset, self.gpu_info.vendor)

        # GPU encoders use different quality parameters
        # NVENC/QSV use -cq (similar to CRF), AMF uses -qp, VideoToolbox uses -q:v
        if self.gpu_info.vendor == GPUVendor.NVIDIA:
            extra_args = ["-preset", gpu_preset_map, "-cq", str(crf)]
        elif self.gpu_info.vendor == GPUVendor.INTEL:
            extra_args = ["-preset", gpu_preset_map, "-global_quality", str(crf)]
        elif self.gpu_info.vendor == GPUVendor.AMD:
            extra_args = ["-quality", gpu_preset_map, "-qp_i", str(crf)]
        elif self.gpu_info.vendor == GPUVendor.APPLE:
            extra_args = ["-q:v", str(crf)]
        else:
            extra_args = ["-preset", gpu_preset_map, "-crf", str(crf)]

        return hw_encoder, gpu_preset_map, crf, extra_args

    def _map_preset_to_gpu(self, cpu_preset: str, vendor: GPUVendor) -> str:
        """Map CPU preset to GPU encoder preset."""
        # NVENC presets: p1 (fastest) to p7 (slowest/best)
        # QSV presets: veryfast, faster, fast, medium, slow, slower, veryslow
        # AMF presets: quality, balanced, speed
        # VideoToolbox: no preset, just quality
        
        nvenc_map = {
            "ultrafast": "p1", "superfast": "p2", "veryfast": "p3",
            "faster": "p4", "fast": "p5", "medium": "p6",
            "slow": "p7", "slower": "p7", "veryslow": "p7",
        }
        qsv_map = {
            "ultrafast": "veryfast", "superfast": "veryfast", "veryfast": "veryfast",
            "faster": "faster", "fast": "fast", "medium": "medium",
            "slow": "slow", "slower": "slower", "veryslow": "veryslow",
        }
        amf_map = {
            "ultrafast": "speed", "superfast": "speed", "veryfast": "speed",
            "faster": "speed", "fast": "balanced", "medium": "balanced",
            "slow": "quality", "slower": "quality", "veryslow": "quality",
        }
        
        if vendor == GPUVendor.NVIDIA:
            return nvenc_map.get(cpu_preset, "p6")
        elif vendor == GPUVendor.INTEL:
            return qsv_map.get(cpu_preset, "medium")
        elif vendor == GPUVendor.AMD:
            return amf_map.get(cpu_preset, "balanced")
        else:
            return cpu_preset
