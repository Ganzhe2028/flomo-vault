from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flomo_vault.media import _extract_image, _recover_image_from_cache, sync_media


class FakeClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def refresh_files(self, ids: list[str]) -> dict[str, dict[str, str]]:
        return {value: {"id": value, "url": f"https://example.invalid/{value}.jpg", "path": f"{value}.jpg"} for value in ids}


class MediaTests(unittest.TestCase):
    def test_flomo_api_retries_transient_curl_failure(self) -> None:
        failed = type("Result", (), {"returncode": 35, "stdout": b"", "stderr": b"tls"})()
        succeeded = type(
            "Result", (), {"returncode": 0, "stdout": json.dumps({"code": 0, "data": []}).encode(), "stderr": b""}
        )()
        with patch("flomo_vault.media.subprocess.run", side_effect=[failed, succeeded]) as run, patch(
            "flomo_vault.media.time.sleep"
        ):
            from flomo_vault.media import FlomoApiClient

            payload = FlomoApiClient("token")._request_json("https://example.invalid")
            self.assertEqual(payload["code"], 0)
            self.assertEqual(run.call_count, 2)

    def test_png_is_recovered_from_chromium_cache_entry(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x00IEND" + b"\x00\x00\x00\x00"
        self.assertEqual(_extract_image(b"header" + png + b"footer", ".png"), png)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache"
            cache.mkdir()
            (cache / "entry").write_bytes(b"photo.png\x00metadata" + png + b"footer")
            destination = root / "media" / "photo.png"
            result = _recover_image_from_cache(
                {"name": "photo.png", "size": len(png)}, destination, cache_root=cache
            )
            self.assertIsNotNone(result)
            self.assertEqual(destination.read_bytes(), png)

    def test_incremental_reuse(self) -> None:
        rows = [{"attachment_id": "1", "memo_created_at": "2026-09-18", "path": "1.jpg", "download_status": "not_requested"}]
        with TemporaryDirectory() as temp, patch("flomo_vault.media.FlomoApiClient", FakeClient), patch(
            "flomo_vault.media._download", return_value=("abc", 3)
        ):
            root = Path(temp)
            first = sync_media(rows, root, "token")
            self.assertEqual(first["downloaded"], 1)
            target = root / rows[0]["media_relpath"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"abc")
            rows[0]["download_status"] = "not_requested"
            second = sync_media(rows, root, "token")
            self.assertEqual(second["existing"], 1)
            self.assertEqual(rows[0]["download_status"], "existing")


if __name__ == "__main__":
    unittest.main()
