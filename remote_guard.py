from dataclasses import dataclass
import json
import re
import time
import urllib.request

from config import APP_AUTHOR, REMOTE_CONFIG_URL, __version__


@dataclass
class RemoteStatus:
    active: bool = True
    latest_version: str = __version__
    min_required_version: str = __version__
    download_url: str = ""
    kill_message: str = f"Service is currently unavailable. Please contact the administrator ({APP_AUTHOR})."
    announcement: str = ""


def parse_version_tuple(v_str: str) -> tuple[int, ...]:
    """Parse a semantic version like '2.0.1' into a tuple (2, 0, 1)."""
    parts = re.findall(r"\d+", str(v_str))
    if not parts:
        return (0, 0, 0)
    return tuple(int(p) for p in parts)


def compare_versions(v1: str, v2: str) -> int:
    """
    Returns:
      -1 if v1 < v2
       0 if v1 == v2
       1 if v1 > v2
    """
    t1 = parse_version_tuple(v1)
    t2 = parse_version_tuple(v2)
    max_len = max(len(t1), len(t2))
    t1 += (0,) * (max_len - len(t1))
    t2 += (0,) * (max_len - len(t2))
    if t1 < t2:
        return -1
    if t1 > t2:
        return 1
    return 0


def parse_remote_json(payload_str: str) -> RemoteStatus:
    """Parse JSON string into RemoteStatus safely. Supports camouflaged / stealth keys."""
    try:
        data = json.loads(payload_str)
        if not isinstance(data, dict):
            return RemoteStatus()
        active_val = data.get("service_online", data.get("online", data.get("active", True)))
        latest_v = str(data.get("latest_version", __version__))
        min_v = str(data.get("min_supported_version", data.get("min_required_version", __version__)))
        dl_url = str(data.get("release_url", data.get("download_url", "")))
        msg = str(
            data.get("system_message")
            or data.get("service_notice")
            or data.get("kill_message")
            or f"Service is currently unavailable. Please contact the administrator ({APP_AUTHOR})."
        )
        announcement = str(data.get("notice", data.get("announcement", "")))
        return RemoteStatus(
            active=bool(active_val),
            latest_version=latest_v,
            min_required_version=min_v,
            download_url=dl_url,
            kill_message=msg,
            announcement=announcement,
        )
    except Exception:
        return RemoteStatus()


def fetch_remote_status(url: str = REMOTE_CONFIG_URL, timeout_s: float = 4.0) -> RemoteStatus:
    """
    Fetch remote configuration from URL over HTTP/HTTPS.
    Fails safe (returns default active status) if URL is empty, unreachable, or times out.
    Uses timestamp cache-busting to bypass GitHub Fastly CDN edge caching (max-age=300).
    """
    if not url or not url.startswith("http"):
        return RemoteStatus()
    req_url = url
    sep = "&" if "?" in req_url else "?"
    req_url = f"{req_url}{sep}_t={int(time.time())}"
    req = urllib.request.Request(
        req_url,
        headers={
            "User-Agent": f"BPMC-Scanner/{__version__} ({APP_AUTHOR})",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8", errors="replace")
                return parse_remote_json(body)
    except Exception:
        pass
    return RemoteStatus()


def check_app_authorization(status: RemoteStatus, current_version: str = __version__) -> tuple[bool, str, str]:
    """
    Evaluate RemoteStatus against current app version.
    Returns (is_allowed, status_code, display_message):
      - (False, "KILL_SWITCH", message): App is remotely disabled / shut down.
      - (False, "MANDATORY_UPDATE", message): Build is deprecated; must update to proceed.
      - (True, "OPTIONAL_UPDATE", message): Newer version available, soft recommendation.
      - (True, "OK", ""): Up to date and authorized.
    """
    if not status.active:
        msg = status.kill_message or f"Service is currently unavailable. Please contact the administrator ({APP_AUTHOR})."
        return False, "KILL_SWITCH", msg

    if compare_versions(current_version, status.min_required_version) < 0:
        dl_part = f"\n\nDownload Link:\n{status.download_url}" if status.download_url else ""
        msg = (
            f"A mandatory update (v{status.latest_version}) is required to continue using BPM Court Scanner.\n"
            f"Your current version: v{current_version}{dl_part}"
        )
        return False, "MANDATORY_UPDATE", msg

    if compare_versions(current_version, status.latest_version) < 0:
        dl_part = f"\n\nDownload Link:\n{status.download_url}" if status.download_url else ""
        msg = (
            f"A new update (v{status.latest_version}) is available!\n"
            f"You are currently running: v{current_version}{dl_part}"
        )
        return True, "OPTIONAL_UPDATE", msg

    return True, "OK", status.announcement

