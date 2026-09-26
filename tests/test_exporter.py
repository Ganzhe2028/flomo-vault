from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flomo_vault.database import VaultData
from flomo_vault.exporter import publish_snapshot


def sample_data(content: str = "你好") -> VaultData:
    memo = {
        "memo_uid": "slug-1",
        "slug": "slug-1",
        "content_html": f"<p>{content}</p>",
        "body_text": content,
        "body_markdown": content,
        "created_at": "2026-09-18 10:00:00",
        "updated_at": "2026-09-18 10:00:00",
        "deleted_at": None,
        "is_deleted": False,
        "tags": ["test"],
        "attachment_ids": [],
        "pin": False,
    }
    return VaultData([memo], [], [], [], [], [], {})


class ExporterTests(unittest.TestCase):
    def test_atomic_current_and_diff(self) -> None:
        media = {"total": 0, "downloaded": 0, "existing": 0, "failed": 0, "deferred": 0}
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = publish_snapshot(root, sample_data(), media, retention=2)
            self.assertTrue((root / "current" / "memos.jsonl").is_file())
            self.assertEqual(first["changes"]["added"], 1)
            second = publish_snapshot(root, sample_data("改过"), media, retention=2)
            self.assertEqual(second["changes"]["updated"], 1)
            self.assertEqual(len([p for p in (root / "snapshots").iterdir() if not p.name.startswith(".")]), 2)
            manifest = json.loads((root / "current" / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")

    def test_failed_publish_preserves_current(self) -> None:
        media = {"total": 0, "downloaded": 0, "existing": 0, "failed": 0, "deferred": 0}
        with TemporaryDirectory() as temp:
            root = Path(temp)
            publish_snapshot(root, sample_data(), media)
            old_target = (root / "current").readlink()
            with patch("flomo_vault.exporter._write_jsonl", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    publish_snapshot(root, sample_data("new"), media)
            self.assertEqual((root / "current").readlink(), old_target)
            self.assertEqual(json.loads((root / "current" / "manifest.json").read_text())["counts"]["memos"], 1)


if __name__ == "__main__":
    unittest.main()
