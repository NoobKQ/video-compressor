"""GPU detection and hardware encoder selection."""

import subprocess
import platform
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum


class GPUVendor(Enum):
    NVIDIA = "nvidia"
    INTEL = "intel"
    AMD = "amd"
    APPLE = "apple"
    NONE = "none"


@dataclass
class GPUInfo:
    vendor: GPUVendor
    name: str
    encoders: List[str]  # Available hardware encoders


def detect_gpu() -> GPUInfo:
    """Detect available GPU and hardware encoders."""
    system = platform.system().lower()
    encoders = []
    vendor = GPUVendor.NONE
    name = "Unknown"

    # Check FFmpeg for available hardware encoders
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ffmpeg_encoders = result.stdout
    except Exception:
        ffmpeg_encoders = ""

    # NVIDIA NVENC
    nvenc_encoders = [e for e in ["h264_nvenc", "hevc_nvenc", "av1_nvenc"] if e in ffmpeg_encoders]
    if nvenc_encoders:
        vendor = GPUVendor.NVIDIA
        encoders = nvenc_encoders
        name = "NVIDIA GPU"
        # Try to get GPU name
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip():
                name = result.stdout.strip().split("\n")[0]
        except Exception:
            pass

    # Intel QuickSync
    elif any(e in ffmpeg_encoders for e in ["h264_qsv", "hevc_qsv", "av1_qsv"]):
        vendor = GPUVendor.INTEL
        encoders = [e for e in ["h264_qsv", "hevc_qsv", "av1_qsv"] if e in ffmpeg_encoders]
        name = "Intel QuickSync"

    # AMD AMF
    elif any(e in ffmpeg_encoders for e in ["h264_amf", "hevc_amf"]):
        vendor = GPUVendor.AMD
        encoders = [e for e in ["h264_amf", "hevc_amf"] if e in ffmpeg_encoders]
        name = "AMD GPU"

    # Apple VideoToolbox (macOS)
    elif system == "darwin" and any(e in ffmpeg_encoders for e in ["h264_videotoolbox", "hevc_videotoolbox"]):
        vendor = GPUVendor.APPLE
        encoders = [e for e in ["h264_videotoolbox", "hevc_videotoolbox"] if e in ffmpeg_encoders]
        name = "Apple VideoToolbox"

    return GPUInfo(vendor=vendor, name=name, encoders=encoders)


def get_best_encoder(gpu_info: GPUInfo, codec: str = "h264") -> Optional[str]:
    """Get the best available hardware encoder for a codec."""
    codec = codec.lower()
    
    # Map codec to hardware encoder names
    encoder_map = {
        "h264": {
            GPUVendor.NVIDIA: "h264_nvenc",
            GPUVendor.INTEL: "h264_qsv",
            GPUVendor.AMD: "h264_amf",
            GPUVendor.APPLE: "h264_videotoolbox",
        },
        "hevc": {
            GPUVendor.NVIDIA: "hevc_nvenc",
            GPUVendor.INTEL: "hevc_qsv",
            GPUVendor.AMD: "hevc_amf",
            GPUVendor.APPLE: "hevc_videotoolbox",
        },
        "av1": {
            GPUVendor.NVIDIA: "av1_nvenc",
            GPUVendor.INTEL: "av1_qsv",
        },
    }
    
    vendor_encoders = encoder_map.get(codec, {})
    return vendor_encoders.get(gpu_info.vendor)


def check_hardware_encoder(encoder: str) -> bool:
    """Check if a specific hardware encoder is available in FFmpeg."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return encoder in result.stdout
    except Exception:
        return False