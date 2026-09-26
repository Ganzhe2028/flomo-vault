from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flomo_vault.config import setting


class ConfigTests(unittest.TestCase):
    def test_missing_file_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("flomo_vault.config.CONFIG_PATH", Path(temporary) / "missing.json"):
                self.assertEqual(setting("daily_since", "1970-01-01"), "1970-01-01")

    def test_user_values_are_read_from_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text('{"daily_since":"2024-01-01"}', encoding="utf-8")
            with patch("flomo_vault.config.CONFIG_PATH", path):
                self.assertEqual(setting("daily_since"), "2024-01-01")


if __name__ == "__main__":
    unittest.main()
