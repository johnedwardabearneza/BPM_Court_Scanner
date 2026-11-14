import unittest
from unittest import mock

from remote_guard import (
    RemoteStatus,
    check_app_authorization,
    compare_versions,
    fetch_remote_status,
    parse_remote_json,
    parse_version_tuple,
)


class TestRemoteGuard(unittest.TestCase):
    def test_parse_version_tuple(self):
        self.assertEqual(parse_version_tuple("2.0.0"), (2, 0, 0))
        self.assertEqual(parse_version_tuple("v2.1.3"), (2, 1, 3))
        self.assertEqual(parse_version_tuple("invalid"), (0, 0, 0))

    def test_compare_versions(self):
        self.assertEqual(compare_versions("2.0.0", "2.0.0"), 0)
        self.assertEqual(compare_versions("2.0.0", "2.0.1"), -1)
        self.assertEqual(compare_versions("2.1.0", "2.0.9"), 1)
        self.assertEqual(compare_versions("1.9.9", "2.0.0"), -1)
        self.assertEqual(compare_versions("3.0", "2.9.9"), 1)

    def test_parse_remote_json_valid(self):
        payload = """{
            "active": false,
            "latest_version": "2.1.0",
            "min_required_version": "2.0.5",
            "download_url": "https://example.com/download",
            "kill_message": "License expired. Contact Edward.",
            "announcement": "Maintenance tonight"
        }"""
        status = parse_remote_json(payload)
        self.assertFalse(status.active)
        self.assertEqual(status.latest_version, "2.1.0")
        self.assertEqual(status.min_required_version, "2.0.5")
        self.assertEqual(status.download_url, "https://example.com/download")
        self.assertEqual(status.kill_message, "License expired. Contact Edward.")
        self.assertEqual(status.announcement, "Maintenance tonight")

    def test_parse_remote_json_stealth_keys(self):
        payload = """{
            "service_online": false,
            "latest_version": "2.2.0",
            "min_supported_version": "2.1.0",
            "release_url": "https://example.com/releases",
            "system_message": "Service maintenance in progress.",
            "notice": "All systems nominal"
        }"""
        status = parse_remote_json(payload)
        self.assertFalse(status.active)
        self.assertEqual(status.latest_version, "2.2.0")
        self.assertEqual(status.min_required_version, "2.1.0")
        self.assertEqual(status.download_url, "https://example.com/releases")
        self.assertEqual(status.kill_message, "Service maintenance in progress.")
        self.assertEqual(status.announcement, "All systems nominal")

    def test_parse_remote_json_corrupt_returns_safe_defaults(self):
        status = parse_remote_json("not a json string {{{")
        self.assertTrue(status.active)

    def test_check_app_authorization_kill_switch_active(self):
        status = RemoteStatus(active=False, kill_message="Service shut down by author.")
        allowed, code, msg = check_app_authorization(status, current_version="2.0.0")
        self.assertFalse(allowed)
        self.assertEqual(code, "KILL_SWITCH")
        self.assertIn("Service shut down by author.", msg)

    def test_check_app_authorization_mandatory_update(self):
        status = RemoteStatus(
            active=True,
            latest_version="2.1.0",
            min_required_version="2.1.0",
            download_url="https://dl.example.com",
        )
        allowed, code, msg = check_app_authorization(status, current_version="2.0.0")
        self.assertFalse(allowed)
        self.assertEqual(code, "MANDATORY_UPDATE")
        self.assertIn("mandatory update", msg.lower())
        self.assertIn("https://dl.example.com", msg)

    def test_check_app_authorization_optional_update(self):
        status = RemoteStatus(
            active=True,
            latest_version="2.1.0",
            min_required_version="2.0.0",
            download_url="https://dl.example.com",
        )
        allowed, code, msg = check_app_authorization(status, current_version="2.0.0")
        self.assertTrue(allowed)
        self.assertEqual(code, "OPTIONAL_UPDATE")
        self.assertIn("new update", msg.lower())

    def test_check_app_authorization_up_to_date(self):
        status = RemoteStatus(
            active=True,
            latest_version="2.0.0",
            min_required_version="2.0.0",
        )
        allowed, code, msg = check_app_authorization(status, current_version="2.0.0")
        self.assertTrue(allowed)
        self.assertEqual(code, "OK")

    def test_fetch_remote_status_unreachable_fails_safe(self):
        status = fetch_remote_status(url="http://10.255.255.1/not_real.json", timeout_s=0.5)
        self.assertTrue(status.active)

    def test_fetch_remote_status_empty_url_fails_safe(self):
        status = fetch_remote_status(url="")
        self.assertTrue(status.active)


if __name__ == "__main__":
    unittest.main()

