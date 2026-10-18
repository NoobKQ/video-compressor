"""File handling for different modes: keep, overwrite, backup."""

import shutil
from pathlib import Path
from typing import Optional
from compressor.core import CompressionResult


class FileHandler:
    """Handles file operations based on mode."""

    def __init__(self, config: dict):
        self.config = config
        self.file_config = config.get("file_handling", {})
        self.mode = self.file_config.get("mode", "keep")
        self.backup_dir_name = self.file_config.get("backup_dir", ".backup")
        self.output_dir_name = self.file_config.get("output_dir", "compressed")

    def prepare_output(self, input_path: Path, output_dir: Optional[Path] = None) -> tuple[Path, Path]:
        """
        Prepare output path and handle pre-compression file operations.
        Returns (output_path, backup_path_if_any)
        """
        backup_path = None

        if self.mode == "overwrite":
            output_path = input_path.with_suffix(".mp4")
            # No backup needed, will overwrite directly
        elif self.mode == "backup":
            # Backup dir/file are only created after compression succeeds.
            backup_dir = input_path.parent / self.backup_dir_name
            backup_path = backup_dir / input_path.name
            # Encode to a temp file so FFmpeg never reads and writes the same
            # path (e.g. movie.mp4 in -> movie.mp4 out).
            final_output = input_path.with_suffix(".mp4")
            output_path = final_output.with_name(final_output.stem + ".tmp-compress.mp4")
        else:  # keep
            if output_dir:
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = output_dir / f"{input_path.stem}.mp4"
            else:
                out_dir = input_path.parent / self.output_dir_name
                out_dir.mkdir(parents=True, exist_ok=True)
                output_path = out_dir / f"{input_path.stem}.mp4"

        return output_path, backup_path

    def finalize(self, result: CompressionResult, backup_path: Optional[Path] = None, input_path: Optional[Path] = None) -> CompressionResult:
        """Handle post-compression file operations."""
        if not result.success:
            # Restore from backup if compression failed
            if backup_path and backup_path.exists():
                shutil.move(str(backup_path), str(result.input_path))
            # Clean up failed output
            if result.output_path and result.output_path.exists():
                try:
                    result.output_path.unlink()
                except Exception:
                    pass
            return result

        if self.mode == "backup" and backup_path and result.success and input_path and not result.skipped:
            # Move original to backup, then move the temp encode into place.
            temp_output = result.output_path
            final_output = input_path.with_suffix(".mp4")
            if temp_output is not None and temp_output.exists():
                backup_dir = input_path.parent / self.backup_dir_name
                backup_dir.mkdir(parents=True, exist_ok=True)
                final_backup = backup_dir / input_path.name
                counter = 1
                while final_backup.exists():
                    final_backup = backup_dir / f"{input_path.stem}_{counter}{input_path.suffix}"
                    counter += 1
                shutil.move(str(input_path), str(final_backup))
                # Same -y semantics as before: replace any stale final file.
                if final_output.exists():
                    final_output.unlink()
                shutil.move(str(temp_output), str(final_output))
                result.output_path = final_output
        elif self.mode == "overwrite":
            # Output is already in place (overwrote original)
            pass
        else:  # keep
            # Original preserved, output in output_dir
            pass

        return result

    def get_backup_path(self, input_path: Path) -> Path:
        """Get the backup path for a given input."""
        return input_path.parent / self.backup_dir_name / input_path.name

    def restore_backup(self, input_path: Path) -> bool:
        """Restore original from backup."""
        backup_path = self.get_backup_path(input_path)
        if backup_path.exists():
            shutil.move(str(backup_path), str(input_path))
            return True
        return False