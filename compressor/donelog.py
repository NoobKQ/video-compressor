"""Done log: lets runs resume across days by remembering finished files.

After each successful compression the source file (path + size + mtime) and
a hash of the active settings are recorded in `.compressor-done.json` next
to the input. A later run skips entries that still match. Writes are atomic
(temp file + replace) so kills and crashes cannot corrupt the log.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOG_NAME = ".compressor-done.json"
VERSION = 1


def settings_hash(config: Dict[str, Any]) -> str:
    """Hash the settings that affect output, so changed flags reprocess."""
    enc = config.get("encoding", {})
    vid = config.get("video", {})
    relevant = {
        "codec": enc.get("codec"),
        "preset": enc.get("preset"),
        "crf": enc.get("crf"),
        "max_size_mb": enc.get("max_size_mb"),
        "audio_codec": enc.get("audio_codec"),
        "audio_bitrate": enc.get("audio_bitrate"),
        "audio_threshold": enc.get("audio_threshold"),
        "scale": vid.get("scale"),
        "scale_threshold": vid.get("scale_threshold"),
        "fps": vid.get("fps"),
        "fps_threshold": vid.get("fps_threshold"),
        "keep_aspect": vid.get("keep_aspect"),
        "mode": config.get("file_handling", {}).get("mode"),
        "gpu": [config.get("gpu", {}).get("enabled"),
                config.get("gpu", {}).get("vendor")],
    }
    blob = json.dumps(relevant, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


class DoneLog:
    """Per input-root record of finished files."""

    def __init__(self, root: Path):
        self.path = root / LOG_NAME
        self.files: Dict[str, Dict[str, Any]] = {}
        self._last_stat: Dict[str, Tuple[int, int]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("version") == VERSION:
                files = data.get("files")
                if isinstance(files, dict):
                    self.files = files
        except (OSError, ValueError):
            self.files = {}

    def _save(self) -> None:
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": VERSION, "files": self.files}, f,
                          indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _stat(self, path: Path) -> Optional[Tuple[int, int]]:
        key = str(path)
        if key in self._last_stat:
            return self._last_stat[key]
        try:
            st = path.stat()
            current = (st.st_size, st.st_mtime_ns)
        except OSError:
            return None
        self._last_stat[key] = current
        return current

    def is_done(self, path: Path, settings: str) -> bool:
        """True if this exact file was finished with identical settings."""
        entry = self.files.get(str(path))
        if not entry or entry.get("settings") != settings:
            return False
        current = self._stat(path)
        if current is None:
            return False
        return entry.get("size") == current[0] and entry.get("mtime_ns") == current[1]

    def mark(self, path: Path, settings: str) -> None:
        """Record a finished file. Uses the pre-compression stat if the
        original was already moved away (backup mode)."""
        current = self._stat(path)
        if current is None:
            return
        self.files[str(path)] = {
            "size": current[0],
            "mtime_ns": current[1],
            "settings": settings,
            "completed_at": int(time.time()),
        }
        self._save()
