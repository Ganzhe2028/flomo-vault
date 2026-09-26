from __future__ import annotations

import json
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

from flomo_vault.notion_text import (
    ROOT_PAGE_ID,
    TextArchiveError,
    TextArchiveClient,
    bootstrap,
    load_state,
    memo_content,
    memo_hash,
    memo_blocks,
    sync_archive,
    week_number,
    week_title,
)


class FakeClient:
    def __init__(self) -> None:
        self.pages: dict[tuple[str, str], str] = {}
        self.created: list[tuple[str, str, str | None]] = []
        self.replaced: list[tuple[str, str]] = []

    def validate_root(self) -> None:
        pass

    def create_or_find(self, parent: str, title: str, content: str | None = None, fallback_blocks: object = None) -> tuple[str, str | None]:
        key = (parent, title)
        if key in self.pages:
            return self.pages[key], None
        page_id = f"page-{len(self.pages) + 1}"
        self.pages[key] = page_id
        self.created.append((parent, title, content))
        return page_id, f"https://notion.test/{page_id}"

    def children(self, parent: str) -> list[dict[str, object]]:
        return [
            {"id": page_id, "child_page": {"title": title}}
            for (page_parent, title), page_id in self.pages.items()
            if page_parent == parent
        ]

    def create(self, parent: str, title: str, content: str | None = None, fallback_blocks: object = None) -> dict[str, str]:
        page_id, url = self.create_or_find(parent, title, content)
        return {"id": page_id, "url": url or ""}

    def replace(self, page_id: str, content: str) -> None:
        self.replaced.append((page_id, content))


def memo(uid: str, when: str, body: str = "正文") -> dict[str, object]:
    return {"memo_uid": uid, "created_at": when, "body_text": body, "is_deleted": False}


class TextArchiveTests(unittest.TestCase):
    def test_new_archive_initializes_empty_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeClient()
            result = sync_archive(root, [memo("first", "2026-09-26 12:00:00", "hello")], client)
            self.assertEqual(result["created"], 1)
            self.assertEqual(load_state(root)["memos"]["first"]["page_id"], "page-4")

    def test_markdown_400_falls_back_to_literal_blocks(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"id":"created","url":"https://notion.test/created"}'

        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                raise HTTPError(request.full_url, 400, "invalid", {}, io.BytesIO(b"{}"))
            return Response()

        source = memo("id", "2026-08-01 01:02:03", "literal #tag\n" + "汉" * 2100)
        client = TextArchiveClient("test", opener=opener, sleeper=lambda _: None)
        page = client.create("week", "title", memo_content(source), memo_blocks(source))
        self.assertEqual(page["id"], "created")
        self.assertIn("markdown", requests[0])
        self.assertNotIn("markdown", requests[1])
        body = requests[1]["children"][2]["paragraph"]["rich_text"]
        self.assertEqual("".join(item["text"]["content"] for item in body), source["body_text"])

    def test_ambiguous_create_discovers_committed_page(self) -> None:
        class Ambiguous(TextArchiveClient):
            def __init__(self) -> None:
                self.calls = 0
                self.found = False

            def find_child(self, parent_id: str, title: str) -> str | None:
                return "already-created" if self.found else None

            def create(self, parent_id: str, title: str, content: str | None = None, fallback_blocks: object = None) -> dict[str, object]:
                self.calls += 1
                self.found = True
                raise TextArchiveError("Notion 文字归档请求未确认：TimeoutError")

        client = Ambiguous()
        self.assertEqual(client.create_or_find("week", "memo")[0], "already-created")
        self.assertEqual(client.calls, 1)

    def test_week_boundaries(self) -> None:
        self.assertEqual(week_number(datetime(2026, 8, 1)), 1)
        self.assertEqual(week_title(datetime(2026, 8, 1)), "第 1 周（08-01～08-02）")
        self.assertEqual(week_number(datetime(2026, 8, 3)), 2)
        self.assertEqual(week_title(datetime(2026, 8, 31)), "第 6 周（08-31）")
        self.assertEqual(week_title(datetime(2026, 9, 26)), "第 4 周（09-21～09-27）")

    def test_literal_body_uses_longer_fence(self) -> None:
        content = memo_content(memo("id", "2026-08-01 01:02:03", "#tag\n````\n@link"))
        self.assertIn("`````text\n#tag\n````\n@link\n`````", content)

    def test_bootstrap_and_incremental_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = memo("old", "2026-08-01 06:32:29")
            meta = root / "meta.json"
            mapping = root / "map.jsonl"
            meta.write_text(json.dumps({
                "root_page_id": ROOT_PAGE_ID, "since": "2026-08-01", "memo_count": 1,
                "year_page_id": "year-2026", "month_page_ids": {"2026-08": "month-08"},
                "week_page_ids": {"2026-08": ["week-1"]},
            }), encoding="utf-8")
            mapping.write_text(json.dumps({"memo_uid": "old", "created_at": old["created_at"], "notion_page_id": "old-page"}) + "\n", encoding="utf-8")
            self.assertEqual(bootstrap(root, meta, mapping, [old])["mapped"], 1)
            with self.assertRaises(TextArchiveError):
                bootstrap(root, meta, mapping, [old])
            client = FakeClient()
            new = memo("new", "2026-09-26 12:00:00", "hello")
            another = memo("another", "2026-09-26 13:00:00", "world")
            first = sync_archive(root, [old, new, another], client, workers=2)
            self.assertEqual((first["created"], first["updated"], first["unchanged"]), (2, 0, 1))
            self.assertEqual(len(client.created), 4)  # New month + week + two memos
            self.assertEqual(sync_archive(root, [old, new, another], client, workers=2)["created"], 0)
            changed = memo("new", "2026-09-26 12:00:00", "edited")
            result = sync_archive(root, [old, changed, another], client, workers=2)
            self.assertEqual(result["updated"], 1)
            self.assertEqual(len(client.replaced), 1)
            self.assertEqual(load_state(root)["memos"]["new"]["content_hash"], memo_hash(changed))


if __name__ == "__main__":
    unittest.main()
