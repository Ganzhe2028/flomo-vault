from __future__ import annotations

import plistlib
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from flomo_vault.schedule import HELPER_BUNDLE_ID, LABEL, helper_app_path, plist_payload
from flomo_vault.workflow import (
    AlreadyRunningError,
    exclusive_run,
    mark_scheduled_complete,
    mark_scheduled_success,
    record_foreground_deferral,
    scheduled_run_due,
)


class WorkflowTests(unittest.TestCase):
    def test_exclusive_lock_rejects_overlap(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with exclusive_run(root):
                with self.assertRaises(AlreadyRunningError):
                    with exclusive_run(root):
                        pass

    def test_daily_catch_up_state(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            morning = datetime(2026, 9, 22, 7, 30, tzinfo=timezone)
            due = datetime(2026, 9, 22, 8, 0, tzinfo=timezone)
            self.assertFalse(scheduled_run_due(root, morning))
            self.assertTrue(scheduled_run_due(root, due))
            mark_scheduled_success(root, due)
            self.assertFalse(scheduled_run_due(root, datetime(2026, 9, 22, 12, 0, tzinfo=timezone)))
            self.assertTrue(scheduled_run_due(root, datetime(2026, 9, 23, 8, 0, tzinfo=timezone)))

    def test_six_foreground_deferrals_stop_for_the_day(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone)
            for expected in range(1, 6):
                attempts, gave_up = record_foreground_deferral(root, now)
                self.assertEqual(attempts, expected)
                self.assertFalse(gave_up)
                self.assertTrue(scheduled_run_due(root, now))
            attempts, gave_up = record_foreground_deferral(root, now)
            self.assertEqual(attempts, 6)
            self.assertTrue(gave_up)
            self.assertFalse(scheduled_run_due(root, now))

    def test_degraded_completed_run_does_not_repeat_every_ten_minutes(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone)
            mark_scheduled_complete(root, now, success=False)
            self.assertFalse(scheduled_run_due(root, datetime(2026, 9, 22, 12, 0, tzinfo=timezone)))

    def test_launch_agent_is_daily_and_not_keepalive(self) -> None:
        payload = plist_payload()
        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 8, "Minute": 0})
        self.assertEqual(payload["StartInterval"], 600)
        self.assertTrue(payload["RunAtLoad"])
        self.assertNotIn("KeepAlive", payload)
        self.assertIn("Flomo Vault Helper.app", payload["ProgramArguments"][0])
        self.assertNotIn("Documents", payload["ProgramArguments"][0])
        self.assertEqual(payload["EnvironmentVariables"]["PYTHONHOME"], sys.base_prefix)
        self.assertEqual(HELPER_BUNDLE_ID, "com.flomo.local-vault.helper")
        self.assertTrue(str(helper_app_path()).endswith("Flomo Vault Helper.app"))
        plistlib.dumps(payload)


if __name__ == "__main__":
    unittest.main()
