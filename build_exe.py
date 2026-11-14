"""
Build script to compile BPM Court Scanner into a standalone Windows Executable (.exe)
using PyInstaller.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
EXE_DIR = DIST / "BPM_Court_Scanner"
ZIP_PATH = DIST / "BPM_Court_Scanner.zip"


def build():
    print(f"[1/4] Preparing build directory: {ROOT}")
    os.chdir(ROOT)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onefile",
        "--name",
        "BPM_Court_Scanner",
        "--icon",
        "app_icon.ico",
        "--add-data",
        "app_icon.ico;.",
        "--add-data",
        "app_icon.png;.",
        "--additional-hooks-dir",
        "hooks",
        "--collect-all",
        "playwright",
        "--hidden-import",
        "_overlapped",
        "--hidden-import",
        "_asyncio",
        "--hidden-import",
        "_queue",
        "--hidden-import",
        "asyncio",
        "--hidden-import",
        "playwright.sync_api",
        "--hidden-import",
        "fitz",
        "--hidden-import",
        "gui_ky",
        "--hidden-import",
        "gui_hits",
        "--hidden-import",
        "gui_worker",
        "--hidden-import",
        "nyscef_browser",
        "--hidden-import",
        "ky_scanner",
        "--hidden-import",
        "classifier",
        "--hidden-import",
        "report_builder",
        "--hidden-import",
        "monthly_report",
        "--hidden-import",
        "pdf_parser",
        "--hidden-import",
        "ui_theme",
        "--hidden-import",
        "config",
        "--hidden-import",
        "remote_guard",
        "--hidden-import",
        "updater",
        "gui.py",
    ]

    print("[2/4] Running PyInstaller...")
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(f"ERROR: PyInstaller failed with exit code {res.returncode}")
        sys.exit(res.returncode)

    exe_file = DIST / "BPM_Court_Scanner.exe"
    if exe_file.exists():
        exe_size_mb = exe_file.stat().st_size / (1024 * 1024)
        target_exe = ROOT / "BPM_Court_Scanner.exe"
        try:
            shutil.copy2(exe_file, target_exe)
            print(f"SUCCESS! Updated single executable at: {target_exe} ({exe_size_mb:.1f} MB)")
            # Clean up intermediate build folders so no duplicate .exe files remain
            shutil.rmtree(DIST, ignore_errors=True)
            shutil.rmtree(BUILD, ignore_errors=True)
        except PermissionError:
            print(f"Note: {target_exe} is currently in use/running.")


if __name__ == "__main__":
    build()

