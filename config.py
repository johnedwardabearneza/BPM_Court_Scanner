import base64
import hashlib
import os
from pathlib import Path
import shutil
import sys
import threading

# Cryptographic integrity signature for developer attribution
_APP_SIGNATURE = b"EDUyNHofPi07KD56GD87KDQ/IDs="
_APP_INTEGRITY_HASH = "98041adf725920ac3e4c061b55dddfa2bca92c43a4d1f1aabe84c6597564a15e"


def verify_app_integrity() -> str:
    """Verify application developer attribution integrity. Raises RuntimeError if tampered with."""
    try:
        raw = base64.b64decode(_APP_SIGNATURE)
        author = bytes([b ^ 0x5A for b in raw]).decode("utf-8")
        if hashlib.sha256(author.encode("utf-8")).hexdigest() != _APP_INTEGRITY_HASH:
            raise ValueError("Integrity mismatch")
        return author
    except Exception:
        raise RuntimeError("Application integrity violation: Core developer license signature has been modified or corrupted.")


APP_AUTHOR = verify_app_integrity()
__author__ = APP_AUTHOR
__version__ = "2.1.2"

# Remote configuration & kill-switch URL (can be overridden via BPM_REMOTE_CONFIG_URL)
REMOTE_CONFIG_URL = os.getenv(
    "BPM_REMOTE_CONFIG_URL",
    "https://raw.githubusercontent.com/QWARDZ/BPM-Court-Scanner/main/version.json",
)

# Set by the GUI "Stop" button; checked between cases and while waiting on a Captcha.
STOP_EVENT = threading.Event()

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR


def find_asset(filename: str) -> Path:
    """Find bundled or local asset across BUNDLE_DIR, BASE_DIR, and _internal."""
    candidates = [
        BUNDLE_DIR / filename,
        BASE_DIR / filename,
        BASE_DIR / "_internal" / filename,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return BASE_DIR / filename

DATA_DIR = BASE_DIR / "BPM_Data"
OUTPUT_DIR = DATA_DIR / "output"
PROFILE_DIR = DATA_DIR / ".edge_profile"

# Dedicated, organized downloads directory (works cleanly in both dev and packaged .exe)
_custom_dl = os.getenv("BPM_DOWNLOADS_DIR")
if _custom_dl:
    DOWNLOADS_DIR = Path(_custom_dl).resolve()
else:
    DOWNLOADS_DIR = DATA_DIR / "downloads"

DOWNLOADS_NY_DIR = DOWNLOADS_DIR / "New York"
DOWNLOADS_KY_DIR = DOWNLOADS_DIR / "Kentucky"


def _migrate_legacy_data() -> None:
    """Consolidate scattered runtime folders (output, downloads, .edge_profile) into BPM_Data/."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for legacy_name, target_dir in [
            ("output", OUTPUT_DIR),
            ("downloads", DOWNLOADS_DIR),
            (".edge_profile", PROFILE_DIR),
        ]:
            legacy_path = BASE_DIR / legacy_name
            if legacy_path.is_dir() and legacy_path.resolve() != target_dir.resolve():
                if not target_dir.exists():
                    shutil.move(str(legacy_path), str(target_dir))
                else:
                    # Target already exists; merge contents if needed
                    for item in legacy_path.iterdir():
                        dest_item = target_dir / item.name
                        if not dest_item.exists():
                            shutil.move(str(item), str(dest_item))
                    try:
                        legacy_path.rmdir()
                    except Exception:
                        pass
    except Exception:
        pass


_migrate_legacy_data()

HANDDOWNS_URL = "https://www.nycourts.gov/courts/ad2/handdowns.shtml"
NYSCEF_SEARCH_URL = "https://iapps.courts.state.ny.us/nyscef/CaseSearch"
COUNTY_AD2_VALUE = "96"

CASE_NUMBER_REGEX = r"\b\d{4}-\d{5}(?:\+\d+)?\b"

MSEDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

DEFAULT_CDP_PORT = 9235
MIN_DELAY_SEC = 1.6
MAX_DELAY_SEC = 2.9
CAPTCHA_WAIT_TIMEOUT_SEC = 86400
