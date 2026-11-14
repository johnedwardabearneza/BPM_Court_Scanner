@echo off
cd /d "%~dp0"
if exist "BPM_Court_Scanner.exe" (
    start "" "BPM_Court_Scanner.exe"
) else if exist "dist\BPM_Court_Scanner.exe" (
    start "" "dist\BPM_Court_Scanner.exe"
) else if exist "dist\BPM_Court_Scanner\BPM_Court_Scanner.exe" (
    start "" "dist\BPM_Court_Scanner\BPM_Court_Scanner.exe"
) else (
    start "" pythonw gui.py
)
