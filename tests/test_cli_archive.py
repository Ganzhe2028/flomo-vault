from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flomo_vault.cli import _perform_sync


class DailyArchiveSourceTests(unittest.TestCase):
    def test_archive_receives_full_memos_before_daily_date_filter(self) -> None:
        all_memos = [
            {"memo_uid": "old", "created_at": "2024-01-01 12:00:00"},
            {"memo_uid": "new", "created_at": "2026-09-26 12:00:00"},
        ]
        full = SimpleNamespace(memos=all_memos, attachments=[])
        selected = SimpleNamespace(memos=all_memos[1:], attachments=[])

        @contextmanager
        def snapshot(**kwargs):
            yield Path("/unused-snapshot")

        collected: list[dict[str, object]] = []
        with (
            patch("flomo_vault.cli.consistent_snapshot", snapshot),
            patch("flomo_vault.cli.parse_snapshot", return_value=full),
            patch("flomo_vault.cli.select_data", return_value=selected),
            patch("flomo_vault.cli.sync_media", return_value={"failed": 0}),
            patch("flomo_vault.cli.publish_snapshot", return_value={"status": "complete"}) as publish,
        ):
            _perform_sync(
                vault=Path("/unused-vault"), data_root=Path("/unused"), source_snapshot=None,
                since="2026-08-01", until="2026-09-26", media_types={"image"},
                media_mode="none", workers=1, retention=1, sync_wait=0, reopen=True,
                media_limit=None, ensure_fresh=False, stabilize_timeout=60,
                abort_if_frontmost=True, json_output=True, archive_memos=collected,
            )
        self.assertEqual([memo["memo_uid"] for memo in collected], ["old", "new"])
        self.assertIs(publish.call_args.args[1], selected)


if __name__ == "__main__":
    unittest.main()
