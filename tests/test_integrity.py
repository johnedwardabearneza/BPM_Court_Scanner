import unittest
from unittest import mock

import config


class TestAppIntegrity(unittest.TestCase):
    def test_verified_author_matches_developer_name(self):
        author = config.verify_app_integrity()
        self.assertEqual(author, "John Edward Bearneza")
        self.assertEqual(config.APP_AUTHOR, "John Edward Bearneza")

    def test_tampered_signature_raises_runtime_error(self):
        with mock.patch("config._APP_SIGNATURE", b"tampered_signature_bytes"):
            with self.assertRaises(RuntimeError) as ctx:
                config.verify_app_integrity()
            self.assertIn("Application integrity violation", str(ctx.exception))

    def test_tampered_hash_raises_runtime_error(self):
        with mock.patch("config._APP_INTEGRITY_HASH", "bad_hash_value"):
            with self.assertRaises(RuntimeError) as ctx:
                config.verify_app_integrity()
            self.assertIn("Application integrity violation", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

