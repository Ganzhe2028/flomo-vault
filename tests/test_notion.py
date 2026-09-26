from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from flomo_vault.notion import NotionReader, read_token, store_token, system_trust_urlopen


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class FakeOpener:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, request: object, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        return FakeResponse(self.payloads.pop(0))


def notion_page(page_id: str, memo_id: str) -> dict[str, object]:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": {
            "Created At": {"type": "date", "date": {"start": "2026-09-01"}},
            "Link": {"type": "url", "url": f"https://v.flomoapp.com/mine/?memo_id={memo_id}"},
        },
    }


class NotionTests(unittest.TestCase):
    def test_system_ca_bundle_is_used_for_https(self) -> None:
        with patch("flomo_vault.notion.urlopen") as open_url:
            system_trust_urlopen(object(), timeout=5)
            self.assertEqual(open_url.call_args.kwargs["timeout"], 5)
            self.assertIsNotNone(open_url.call_args.kwargs["context"])

    def test_schema_and_cursor_pagination(self) -> None:
        opener = FakeOpener(
            [
                {"properties": {"Link": {"type": "url"}, "Created At": {"type": "date"}}},
                {"results": [notion_page("p1", "Memo_1")], "has_more": True, "next_cursor": "next"},
                {"results": [notion_page("p2", "Memo_2")], "has_more": False, "next_cursor": None},
            ]
        )
        reader = NotionReader("secret", opener=opener, sleeper=lambda _: None)
        reader.validate_schema()
        rows = reader.query_rows("2026-08-01", "2026-09-22")
        self.assertEqual([row["id"] for row in rows], ["p1", "p2"])
        second_query = json.loads(opener.requests[2][0].data.decode())
        self.assertEqual(second_query["start_cursor"], "next")

    def test_keychain_commands_do_not_print_token(self) -> None:
        completed = type("Result", (), {"returncode": 0, "stdout": "saved-token\n", "stderr": ""})()
        with patch("flomo_vault.notion.KEYCHAIN_ACCOUNT", "test-data-source"), patch("flomo_vault.notion.subprocess.run", return_value=completed) as run:
            self.assertEqual(read_token(), "saved-token")
            store_token("saved-token")
            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
