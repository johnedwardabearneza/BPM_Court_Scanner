"""
Custom PyInstaller hook for PyMuPDF (fitz).
Collects runtime DLLs, submodules, and essential metadata, while strictly
excluding the bulky mupdf-devel C header files that cause extraction failures
under Windows Defender / antivirus inspection.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

binaries = collect_dynamic_libs("pymupdf")
hiddenimports = collect_submodules("pymupdf") + ["fitz", "pymupdf"]

raw_datas = collect_data_files("pymupdf")
datas = [d for d in raw_datas if "mupdf-devel" not in d[0]]

