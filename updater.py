"""
In-app Auto-Updater for BPM Court Scanner.
Implements the atomic Rename-and-Replace pattern for standalone Windows executables.
"""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

from config import APP_AUTHOR, __version__


def cleanup_old_version() -> None:
    """Delete any leftover .old.exe binary after an in-place auto-update restart."""
    if not getattr(sys, "frozen", False):
        return
    try:
        current_exe = Path(sys.executable).resolve()
        old_exe = current_exe.with_name(f"{current_exe.stem}.old.exe")
        if old_exe.exists():
            # Retry deletion up to 3 times in case the old process just released the handle
            for _ in range(3):
                try:
                    old_exe.unlink()
                    break
                except PermissionError:
                    time.sleep(0.5)
    except Exception:
        pass


def is_valid_windows_executable(path: Path) -> bool:
    """Verify downloaded binary exists, is at least 15MB, and begins with Windows PE 'MZ' magic bytes."""
    try:
        if not path.is_file() or path.stat().st_size < 15 * 1024 * 1024:
            return False
        with open(path, "rb") as f:
            header = f.read(2)
            return header == b"MZ"
    except Exception:
        return False


def download_update_binary(
    url: str,
    dest_path: Path,
    progress_callback=None,
    stop_event: threading.Event | None = None,
    timeout_s: float = 30.0,
) -> bool:
    """
    Download the new executable binary in chunks over HTTP with progress reporting.
    Returns True if successfully downloaded and validated, False otherwise.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"BPMC-AutoUpdater/{__version__} ({APP_AUTHOR})",
            "Cache-Control": "no-cache",
        },
    )
    temp_path = dest_path.with_name(f"{dest_path.name}.part")
    try:
        if temp_path.exists():
            temp_path.unlink()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 128 * 1024  # 128 KB chunks

            with open(temp_path, "wb") as out_file:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        temp_path.unlink(missing_ok=True)
                        return False
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size)

        if not is_valid_windows_executable(temp_path):
            temp_path.unlink(missing_ok=True)
            return False

        if dest_path.exists():
            dest_path.unlink()
        temp_path.rename(dest_path)
        return True
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return False


def apply_update_and_restart(new_exe_path: Path) -> None:
    """
    Atomically swap the currently running executable with the new binary and restart.
    1. Rename running BPM_Court_Scanner.exe -> BPM_Court_Scanner.old.exe
    2. Rename downloaded BPM_Court_Scanner.new.exe -> BPM_Court_Scanner.exe
    3. Launch the new BPM_Court_Scanner.exe in a detached process.
    4. Exit the current process immediately so Windows releases file lock.
    """
    if not is_valid_windows_executable(new_exe_path):
        raise ValueError("Cannot apply update: downloaded binary is corrupted or invalid.")

    if not getattr(sys, "frozen", False):
        # Development mode: Cannot swap python.exe, but we can launch the new exe for testing
        subprocess.Popen([str(new_exe_path)])
        return

    current_exe = Path(sys.executable).resolve()
    old_exe = current_exe.with_name(f"{current_exe.stem}.old.exe")

    # Clean up previous old binary if present
    if old_exe.exists():
        try:
            old_exe.unlink()
        except Exception:
            pass

    # Windows Rename Trick: A running binary CAN be renamed even while in execution!
    current_exe.rename(old_exe)
    new_exe_path.rename(current_exe)

    # Spawn the newly updated executable in a detached subprocess
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

    subprocess.Popen([str(current_exe)], creationflags=creation_flags, close_fds=True)

    # Terminate old process immediately
    os._exit(0)

