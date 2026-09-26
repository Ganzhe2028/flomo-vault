from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flomo_vault.app_control import FlomoInUseError, consistent_snapshot, frontmost_bundle_id, open_flomo, quit_flomo
from flomo_vault.htmltext import html_to_text
from flomo_vault.local_storage import decode_chromium_string
from flomo_vault.media import media_relative_path, signed_query


class CoreTests(unittest.TestCase):
    def test_chromium_strings(self) -> None:
        self.assertEqual(decode_chromium_string(b"\x01hello"), "hello")
        self.assertEqual(decode_chromium_string(b"\x00" + "中文".encode("utf-16-le")), "中文")

    def test_html_to_text(self) -> None:
        self.assertEqual(html_to_text("<p>第一行<br>第二行</p><ul><li>A</li></ul>"), "第一行\n第二行\n\n- A")

    def test_signature_is_deterministic(self) -> None:
        actual = signed_query([("ids[]", "2"), ("ids[]", "1")], "5.26.72", timestamp=123)
        unsigned, sign = actual.rsplit("&sign=", 1)
        self.assertEqual(sign, hashlib.md5((unsigned + "dbbc3dd73364b4084c3a69346e0ce2b2").encode()).hexdigest())
        self.assertIn("ids%5B%5D=2", unsigned)

    def test_media_path(self) -> None:
        row = {"attachment_id": "123", "memo_created_at": "2026-09-18 12:00:00", "path": "file/x/photo.jpg"}
        self.assertEqual(media_relative_path(row, {}).as_posix(), "media/2026/2026-09/123.jpg")

    def test_existing_snapshot_is_not_copied_or_removed(self) -> None:
        with TemporaryDirectory() as temp:
            source = Path(temp)
            with consistent_snapshot(source_snapshot=source) as yielded:
                self.assertEqual(yielded, source)
            self.assertTrue(source.exists())

    def test_live_app_is_reopened_after_copy(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "IndexedDB").mkdir()
            (root / "Local Storage").mkdir()
            with patch("flomo_vault.app_control.is_flomo_running", return_value=True), patch(
                "flomo_vault.app_control.quit_flomo"
            ) as quit_app, patch("flomo_vault.app_control.open_flomo") as open_app:
                with consistent_snapshot(data_root=root, sync_wait=0) as snapshot:
                    self.assertTrue((snapshot / "IndexedDB").is_dir())
                    open_app.assert_called_once()
                quit_app.assert_called_once()

    def test_daily_snapshot_restores_initially_closed_app(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "IndexedDB").mkdir()
            (root / "Local Storage").mkdir()
            running = {"value": False}

            def open_app(*, background: bool = False) -> None:
                del background
                running["value"] = True

            def quit_app() -> None:
                running["value"] = False

            with patch("flomo_vault.app_control.is_flomo_running", side_effect=lambda: running["value"]), patch(
                "flomo_vault.app_control.open_flomo", side_effect=open_app
            ) as opened, patch("flomo_vault.app_control.quit_flomo", side_effect=quit_app) as quit_called, patch(
                "flomo_vault.app_control.wait_for_flomo"
            ), patch("flomo_vault.app_control.wait_for_data_stable"):
                with consistent_snapshot(data_root=root, ensure_fresh=True) as snapshot:
                    self.assertTrue((snapshot / "IndexedDB").is_dir())
                self.assertFalse(running["value"])
                opened.assert_called_once()
                quit_called.assert_called_once()

    def test_scheduled_snapshot_never_quits_frontmost_flomo(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "IndexedDB").mkdir()
            (root / "Local Storage").mkdir()
            with patch("flomo_vault.app_control.is_flomo_running", return_value=True), patch(
                "flomo_vault.app_control.is_flomo_frontmost", return_value=True
            ), patch("flomo_vault.app_control.quit_flomo") as quit_app:
                with self.assertRaises(FlomoInUseError):
                    with consistent_snapshot(data_root=root, ensure_fresh=True, abort_if_frontmost=True):
                        pass
                quit_app.assert_not_called()

    def test_scheduled_snapshot_rechecks_foreground_before_quit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "IndexedDB").mkdir()
            (root / "Local Storage").mkdir()
            with patch("flomo_vault.app_control.is_flomo_running", return_value=True), patch(
                "flomo_vault.app_control.is_flomo_frontmost", side_effect=[False, True]
            ), patch("flomo_vault.app_control.wait_for_data_stable"), patch(
                "flomo_vault.app_control.quit_flomo"
            ) as quit_app:
                with self.assertRaises(FlomoInUseError):
                    with consistent_snapshot(data_root=root, ensure_fresh=True, abort_if_frontmost=True):
                        pass
                quit_app.assert_not_called()

    def test_frontmost_app_uses_workspace_without_accessibility(self) -> None:
        result = type("Result", (), {"returncode": 0, "stdout": "com.flomoapp.m\n"})()
        with patch("flomo_vault.app_control.subprocess.run", return_value=result) as run:
            self.assertEqual(frontmost_bundle_id(), "com.flomoapp.m")
            self.assertIn("JavaScript", run.call_args.args[0])

    def test_quit_uses_appkit_instead_of_apple_events(self) -> None:
        result = type("Result", (), {"returncode": 0, "stdout": ""})()
        with patch("flomo_vault.app_control.subprocess.run", return_value=result) as run, patch(
            "flomo_vault.app_control.is_flomo_running", return_value=False
        ):
            quit_flomo()
        arguments = run.call_args_list[0].args[0]
        self.assertIn("JavaScript", arguments)
        self.assertIn("NSRunningApplication", arguments[-1])
        self.assertNotIn("tell application", arguments[-1])

    def test_background_open_is_hidden_and_restores_focus(self) -> None:
        result = type("Result", (), {"returncode": 0, "stdout": "com.openai.codex\n"})()
        with patch("flomo_vault.app_control.subprocess.run", return_value=result) as run, patch(
            "flomo_vault.app_control.is_flomo_running", return_value=True
        ):
            open_flomo(background=True)
        launch_arguments = run.call_args_list[1].args[0]
        self.assertIn("-g", launch_arguments)
        self.assertIn("-j", launch_arguments)
        restore_script = run.call_args_list[2].args[0][-1]
        self.assertIn("hide()", restore_script)
        self.assertIn("com.openai.codex", restore_script)


if __name__ == "__main__":
    unittest.main()
