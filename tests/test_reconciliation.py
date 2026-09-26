from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from flomo_vault.reconciliation import (
    build_reconciliation,
    extract_memo_uid,
    normalize_input,
    publish_reconciliation,
)


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {"run_id": "local-run"}
        self.memos = [{"memo_uid": "Memo_1"}, {"memo_uid": "Memo_2"}]

    def test_extracts_only_canonical_flomo_links(self) -> None:
        self.assertEqual(
            extract_memo_uid("https://v.flomoapp.com/mine/?memo_id=Memo_1"),
            ("Memo_1", None),
        )
        self.assertEqual(extract_memo_uid(""), (None, "blank_link"))
        self.assertEqual(extract_memo_uid("https://example.com/?memo_id=Memo_1"), (None, "invalid_link"))
        self.assertEqual(
            extract_memo_uid("https://v.flomoapp.com/mine/?memo_id=bad%20id"),
            (None, "invalid_memo_id"),
        )

    def test_exact_pending_notion_only_and_duplicate(self) -> None:
        rows = normalize_input(
            {
                "results": [
                    {"id": "p1", "url": "n1", "Link": "https://v.flomoapp.com/mine/?memo_id=Memo_1"},
                    {"id": "p3", "url": "n3", "Link": "https://v.flomoapp.com/mine/?memo_id=Memo_3"},
                ]
            }
        )
        report, mapping = build_reconciliation(self.manifest, self.memos, rows, "test")
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["counts"]["pending_notion"], 1)
        self.assertEqual(report["notion_only_memo_uids"], ["Memo_3"])
        self.assertEqual([row["match_status"] for row in mapping], ["matched", "pending_notion"])

        duplicate = rows + [{"id": "p4", "url": "n4", "link": "https://v.flomoapp.com/mine/?memo_id=Memo_1"}]
        report, mapping = build_reconciliation(self.manifest, self.memos, duplicate, "test")
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["duplicate_memo_uids"], ["Memo_1"])
        self.assertEqual(mapping[0]["match_status"], "duplicate_notion")

    def test_unavailable_and_atomic_publish(self) -> None:
        report, mapping = build_reconciliation(
            self.manifest,
            self.memos,
            [],
            "notion-api",
            error="auth failed",
            now=datetime(2026, 9, 22, tzinfo=timezone.utc),
        )
        self.assertEqual(report["status"], "unavailable")
        self.assertTrue(all(row["match_status"] == "notion_unavailable" for row in mapping))
        with TemporaryDirectory() as temp:
            root = Path(temp)
            publish_reconciliation(root, report, mapping)
            self.assertTrue((root / "reconciliation" / "current").is_symlink())
            loaded = json.loads((root / "reconciliation" / "current" / "reconcile.json").read_text())
            self.assertEqual(loaded["status"], "unavailable")
            with (root / "reconciliation" / "current" / "memo_notion_map.jsonl").open() as handle:
                self.assertEqual(sum(1 for _ in handle), 2)


if __name__ == "__main__":
    unittest.main()
