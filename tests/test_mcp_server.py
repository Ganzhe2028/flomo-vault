from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from mcp import Client, StdioServerParameters

from flomo_vault import __version__
from flomo_vault.mcp_server import VaultStore, VaultTools, create_server


FIXTURE = Path(__file__).parent / "fixtures" / "vault"


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "vault"
        shutil.copytree(FIXTURE, self.root, symlinks=True)
        self.tools = VaultTools(VaultStore(self.root), today=date(2026, 10, 1))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _snapshot_path(self) -> Path:
        return (self.root / "current").resolve()

    def _new_run_id(self, run_id: str) -> None:
        path = self._snapshot_path() / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["run_id"] = run_id
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_all_tools_and_deleted_rules(self) -> None:
        status = self.tools.get_vault_status()
        self.assertEqual(status["run_id"], "fixture-run-1")
        self.assertEqual(status["counts"]["memos"], 12)
        self.assertEqual(self.tools.search_memos("swimming")["total"], 2)
        self.assertEqual(self.tools.search_memos("swimming")["memos"][0]["memo_uid"], "a")
        self.assertEqual(self.tools.search_memos("swimming memo", include_deleted=True)["total"], 1)
        self.assertEqual(self.tools.search_memos("swimming ideas")["total"], 1)
        self.assertEqual(self.tools.search_memos("swimming", tag="sport", since="2026-09-14", until="2026-09-15")["total"], 2)
        self.assertEqual(self.tools.list_memos()["total"], 11)
        self.assertEqual(self.tools.list_memos(include_deleted=True)["total"], 12)
        self.assertEqual(self.tools.list_memos(order="asc")["memos"][0]["memo_uid"], "k")
        self.assertEqual(self.tools.get_memo("deleted")["error"]["code"], "memo_not_found")
        self.assertTrue(self.tools.get_memo("deleted", include_deleted=True)["memo"]["is_deleted"])
        tags = self.tools.list_tags()["tags"]
        self.assertEqual(next(tag["memo_count"] for tag in tags if tag["name"] == "sport"), 4)
        stats = self.tools.get_stats()
        self.assertEqual(stats["active_memos"], 11)
        self.assertEqual(stats["relations"], {"memo_edges": 5, "bidirectional_pairs": 1, "external_links": 1})
        self.assertEqual(stats["memos_with_attachments"], 1)
        self.assertAlmostEqual(stats["attachment_memo_ratio"], 1 / 11)
        self.assertEqual(len(stats["monthly_memos"]), 24)
        self.assertEqual(len(stats["hourly_memos"]), 24)
        self.assertEqual(next(row["count"] for row in stats["monthly_memos"] if row["month"] == "2026-09"), 2)

    def test_attachment_join_and_safe_absolute_paths(self) -> None:
        memo = self.tools.get_memo("a")["memo"]
        self.assertEqual(len(memo["attachments"]), 2)
        image, missing = memo["attachments"]
        self.assertEqual(image["local_path"], str((self.root / "media/2026/09/image.jpg").resolve()))
        self.assertIsNone(missing["local_path"])
        self.assertEqual(memo["body_text"], "Swimming ideas and laps")

    def test_rejects_attachment_path_outside_media(self) -> None:
        path = self._snapshot_path() / "attachments.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["media_relpath"] = "../outside.jpg"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.assertIsNone(self.tools.get_memo("a")["memo"]["attachments"][0]["local_path"])

    def test_rejects_current_symlink_outside_snapshots(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        shutil.copytree(self._snapshot_path(), outside)
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(outside)
        result = VaultTools(VaultStore(self.root)).get_vault_status()
        self.assertEqual(result["error"]["code"], "vault_unreadable")

    def test_context_directions_depth_and_deleted_neighbors(self) -> None:
        context = self.tools.get_memo_context("a")
        self.assertEqual([row["memo_uid"] for row in context["outgoing"]], ["c"])
        self.assertEqual([row["memo_uid"] for row in context["incoming"]], ["d"])
        self.assertEqual([row["memo_uid"] for row in context["bidirectional"]], ["b"])
        self.assertEqual(context["unavailable"]["outgoing"], [{"memo_uid": "deleted", "reason": "deleted"}])
        self.assertEqual(context["second_hop"], [])
        deep = self.tools.get_memo_context("a", depth=2)
        self.assertEqual([row["memo_uid"] for row in deep["second_hop"]], ["e"])
        self.assertEqual(deep["second_hop"][0]["via"], ["c"])
        self.assertFalse(deep["truncated"])
        self.assertEqual(self.tools.get_memo_context("deleted")["error"]["code"], "memo_not_found")
        deleted_center = self.tools.get_memo_context("deleted", include_deleted=True)
        self.assertTrue(deleted_center["center"]["is_deleted"])
        self.assertEqual([row["memo_uid"] for row in deleted_center["incoming"]], ["a"])
        self.assertFalse(any(row["memo_uid"] == "deleted" for key in ("outgoing", "incoming", "bidirectional") for row in deep[key]))

    def test_context_reports_references_outside_snapshot(self) -> None:
        snapshot = self._snapshot_path()
        memo_path = snapshot / "memos.jsonl"
        rows = [json.loads(line) for line in memo_path.read_text(encoding="utf-8").splitlines()]
        rows[0]["outgoing_memo_uids"].append("outside")
        rows[0]["outgoing_memo_uids"].append("e")  # Present, but missing its link row.
        memo_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        link_path = snapshot / "links.jsonl"
        links = [json.loads(line) for line in link_path.read_text(encoding="utf-8").splitlines()]
        links.append({"kind": "memo", "source_memo_uid": "a", "target_memo_uid": "outside"})
        link_path.write_text("".join(json.dumps(row) + "\n" for row in links), encoding="utf-8")
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["counts"]["links"] = len(links)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        context = self.tools.get_memo_context("a")
        self.assertEqual(context["unavailable"]["outgoing"], [
            {"memo_uid": "deleted", "reason": "deleted"},
            {"memo_uid": "e", "reason": "missing_link"},
            {"memo_uid": "outside", "reason": "outside_snapshot"},
        ])
        represented = {
            row["memo_uid"] for key in ("outgoing", "bidirectional") for row in context[key]
        } | {row["memo_uid"] for row in context["unavailable"]["outgoing"]}
        self.assertEqual(represented, set(context["center"]["outgoing_memo_uids"]))
        self.assertFalse(context["truncated"])

    def test_context_caps_unique_neighbors_at_fifty(self) -> None:
        snapshot = self._snapshot_path()
        memo_path = snapshot / "memos.jsonl"
        link_path = snapshot / "links.jsonl"
        memos = [json.loads(line) for line in memo_path.read_text(encoding="utf-8").splitlines()]
        links = [json.loads(line) for line in link_path.read_text(encoding="utf-8").splitlines()]
        for index in range(55):
            uid = f"extra-{index:02d}"
            memos.append({**memos[0], "memo_uid": uid, "slug": uid, "attachment_ids": []})
            links.append({"link_id": 100 + index, "kind": "memo", "source_memo_uid": "a", "target_memo_uid": uid, "target_url": "", "is_bidirectional": False})
        memo_path.write_text("".join(json.dumps(row) + "\n" for row in memos), encoding="utf-8")
        link_path.write_text("".join(json.dumps(row) + "\n" for row in links), encoding="utf-8")
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["run_id"] = "fixture-expanded"
        manifest["counts"]["memos"] = len(memos)
        manifest["counts"]["links"] = len(links)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        context = self.tools.get_memo_context("a", depth=2)
        self.assertEqual(sum(len(context[key]) for key in ("outgoing", "incoming", "bidirectional", "second_hop")), 50)
        self.assertTrue(context["truncated"])

    def test_pagination_and_invalid_parameters(self) -> None:
        result = self.tools.list_memos(limit=3, offset=2)
        self.assertEqual((len(result["memos"]), result["total"], result["has_more"]), (3, 11, True))
        self.assertFalse(self.tools.list_memos(limit=3, offset=10)["has_more"])
        self.assertEqual(self.tools.list_memos(limit=0)["error"]["code"], "invalid_param")
        self.assertEqual(self.tools.search_memos(" ")["error"]["code"], "invalid_param")
        self.assertEqual(self.tools.list_memos(since="2026-10-01", until="2026-09-01")["error"]["code"], "invalid_date_range")
        self.assertEqual(self.tools.list_memos(since="2026-02-30")["error"]["code"], "invalid_param")
        self.assertEqual(self.tools.get_memo_context("a", depth=3)["error"]["code"], "invalid_param")
        self.assertEqual(self.tools.get_memo("missing")["error"]["code"], "memo_not_found")

    def test_stats_warn_when_older_memos_fall_outside_month_chart(self) -> None:
        memo_path = self._snapshot_path() / "memos.jsonl"
        rows = [json.loads(line) for line in memo_path.read_text(encoding="utf-8").splitlines()]
        rows[0]["created_at"] = "2020-01-01 09:00:00"
        memo_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        stats = self.tools.get_stats()
        self.assertEqual(stats["active_memos"], 11)
        self.assertEqual(sum(row["count"] for row in stats["monthly_memos"]), 10)
        self.assertEqual(stats["monthly_window_excluded_memos"], 1)
        self.assertEqual(stats["earliest_excluded_month"], "2020-01")
        self.assertIn("1 active memo", stats["warnings"][0])

    def test_search_and_list_do_not_return_full_bodies(self) -> None:
        snapshot = self._snapshot_path()
        memo_path = snapshot / "memos.jsonl"
        template = json.loads(memo_path.read_text(encoding="utf-8").splitlines()[0])
        rows = [
            {**template, "memo_uid": f"long-{index:03d}", "slug": f"long-{index:03d}",
             "body_text": "alpha " + "笔" * 5000, "body_markdown": "alpha " + "笔" * 5000}
            for index in range(100)
        ]
        memo_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["counts"]["memos"] = len(rows)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        for result in (self.tools.list_memos(limit=100), self.tools.search_memos("alpha", limit=100)):
            self.assertEqual(result["total"], 100)
            self.assertLess(len(json.dumps(result, ensure_ascii=False, indent=2).encode()), 25_000)
            self.assertNotIn("body_markdown", result["memos"][0])
        self.assertEqual(len(self.tools.get_memo("long-000")["memo"]["body_markdown"]), 5006)

    def test_mixed_timestamp_formats_sort_chronologically(self) -> None:
        path = self._snapshot_path() / "memos.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["created_at"] = "2026-09-15T09:00:00+08:00"
        rows[1]["created_at"] = "2026-09-15T01:30:00+00:00"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.assertEqual([row["memo_uid"] for row in self.tools.list_memos(limit=2)["memos"]], ["b", "a"])

    def test_missing_empty_corrupt_and_stale(self) -> None:
        missing = VaultTools(VaultStore(self.root / "not-there"))
        calls = (
            lambda tools: tools.get_vault_status(),
            lambda tools: tools.search_memos("swimming"),
            lambda tools: tools.get_memo("a"),
            lambda tools: tools.get_memo_context("a"),
            lambda tools: tools.list_memos(),
            lambda tools: tools.list_tags(),
            lambda tools: tools.get_stats(),
        )
        for call in calls:
            self.assertEqual(call(missing)["error"]["code"], "vault_not_found")
        empty_root = self.root / "empty"
        empty_root.mkdir()
        empty = VaultTools(VaultStore(empty_root))
        for call in calls:
            self.assertEqual(call(empty)["error"]["code"], "vault_empty")
        manifest_path = self._snapshot_path() / "manifest.json"
        manifest_path.write_text("not json", encoding="utf-8")
        self.assertEqual(VaultTools(VaultStore(self.root)).get_vault_status()["error"]["code"], "manifest_unreadable")
        self.assertEqual(self.tools.get_vault_status()["error"]["code"], "manifest_unreadable")
        invalid_manifest = json.loads((FIXTURE / "snapshots/fixture-run-1/manifest.json").read_text(encoding="utf-8"))
        invalid_manifest["counts"] = []
        manifest_path.write_text(json.dumps(invalid_manifest), encoding="utf-8")
        self.assertEqual(VaultTools(VaultStore(self.root)).get_vault_status()["error"]["code"], "manifest_unreadable")
        manifest_path.write_text((FIXTURE / "snapshots/fixture-run-1/manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(self.tools.get_vault_status()["run_id"], "fixture-run-1")
        manifest_path.write_text("not json", encoding="utf-8")
        stale = self.tools.get_vault_status()
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["run_id"], "fixture-run-1")

    def test_bad_line_is_skipped_and_warning_returned(self) -> None:
        path = self._snapshot_path() / "memos.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[-1] = "{bad json"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._new_run_id("fixture-run-2")
        result = self.tools.list_memos()
        self.assertEqual(result["total"], 11)
        self.assertIn("skipped 1", result["warnings"][0])
        self.assertNotIn("stale", result)

    def test_run_id_change_reloads_memos_in_place(self) -> None:
        self.assertEqual(self.tools.list_memos()["total"], 11)
        path = self._snapshot_path() / "memos.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows.append({**rows[0], "memo_uid": "new", "slug": "new"})
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self._new_run_id("fixture-run-2")
        manifest_path = self._snapshot_path() / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["counts"]["memos"] = 13
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.tools.list_memos()["total"], 12)

    def test_truncated_snapshot_uses_last_good_data(self) -> None:
        self.assertEqual(self.tools.list_memos()["total"], 11)
        path = self._snapshot_path() / "memos.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:6]) + "\n", encoding="utf-8")
        self._new_run_id("fixture-run-2")
        result = self.tools.list_memos()
        self.assertTrue(result["stale"])
        self.assertEqual(result["total"], 11)

    def test_status_warns_for_partial_and_selected_snapshot(self) -> None:
        path = self._snapshot_path() / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["run_id"] = "fixture-partial"
        manifest["status"] = "partial_media"
        manifest["media"]["failed"] = 1
        manifest["selection"] = {"since": "2026-01-01"}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        status = self.tools.get_vault_status()
        self.assertIn("1 attachments failed", status["warning"])
        self.assertIn("bounded selection", status["warnings"][0])
        self.assertIn("selected vault snapshot", self.tools.get_stats()["warnings"][0])

    def test_symlink_retarget_and_unreadable_snapshot_fallback(self) -> None:
        self.assertEqual(self.tools.list_memos()["total"], 11)
        next_path = self.root / "snapshots" / "fixture-run-2"
        shutil.copytree(self._snapshot_path(), next_path)
        manifest_path = next_path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["run_id"] = "fixture-run-2"
        manifest["counts"]["memos"] = 13
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        memo_path = next_path / "memos.jsonl"
        rows = [json.loads(line) for line in memo_path.read_text(encoding="utf-8").splitlines()]
        rows.append({**rows[0], "memo_uid": "new", "slug": "new"})
        memo_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        new_link = self.root / ".current-new"
        new_link.symlink_to("snapshots/fixture-run-2")
        os.replace(new_link, self.root / "current")
        self.assertEqual(self.tools.list_memos()["total"], 12)
        broken_path = self.root / "snapshots" / "fixture-run-3"
        shutil.copytree(next_path, broken_path)
        broken_manifest_path = broken_path / "manifest.json"
        broken_manifest = json.loads(broken_manifest_path.read_text(encoding="utf-8"))
        broken_manifest["run_id"] = "fixture-run-3"
        broken_manifest_path.write_text(json.dumps(broken_manifest), encoding="utf-8")
        (broken_path / "memos.jsonl").unlink()
        broken_link = self.root / ".current-broken"
        broken_link.symlink_to("snapshots/fixture-run-3")
        os.replace(broken_link, self.root / "current")
        stale = self.tools.list_memos()
        self.assertEqual(stale["total"], 12)
        self.assertTrue(stale["stale"])

    def test_reads_do_not_change_vault_mtimes(self) -> None:
        before = {str(path.relative_to(self.root)): path.stat().st_mtime_ns for path in self.root.rglob("*")}
        self.tools.get_vault_status()
        self.tools.search_memos("swimming")
        self.tools.get_memo("a")
        self.tools.get_memo_context("a", depth=2)
        self.tools.list_memos()
        self.tools.list_tags()
        self.tools.get_stats()
        after = {str(path.relative_to(self.root)): path.stat().st_mtime_ns for path in self.root.rglob("*")}
        self.assertEqual(after, before)

    def test_sdk_registration_and_real_stdio(self) -> None:
        async def in_process() -> None:
            async with Client(create_server(VaultStore(self.root))) as client:
                self.assertEqual(len((await client.list_tools()).tools), 7)
                result = await client.call_tool("search_memos", {"query": "swimming"})
                self.assertEqual(result.structured_content["total"], 2)

        async def stdio() -> None:
            env = dict(os.environ)
            env["FLOMO_VAULT_ROOT"] = str(self.root)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            params = StdioServerParameters(command=sys.executable, args=["-m", "flomo_vault.mcp_server"], env=env)
            async with Client(params) as client:
                self.assertEqual(client.server_info.version, __version__)
                names = {tool.name for tool in (await client.list_tools()).tools}
                self.assertEqual(names, {"get_vault_status", "search_memos", "get_memo", "get_memo_context", "list_memos", "list_tags", "get_stats"})
                calls = (
                    ("get_vault_status", {}), ("search_memos", {"query": "swimming"}),
                    ("get_memo", {"memo_uid": "a"}), ("get_memo_context", {"memo_uid": "a"}),
                    ("list_memos", {}), ("list_tags", {}), ("get_stats", {}),
                )
                for name, args in calls:
                    result = await client.call_tool(name, args)
                    self.assertFalse(result.is_error, name)
                    self.assertTrue(result.structured_content["ok"], name)
                invalid_calls = (
                    ("list_memos", {"limit": 2.5}), ("list_memos", {"limit": "5"}),
                    ("get_memo_context", {"memo_uid": "a", "depth": "2"}),
                    ("search_memos", {"query": 5}), ("get_memo", {}),
                )
                for name, args in invalid_calls:
                    result = await client.call_tool(name, args)
                    self.assertFalse(result.is_error, (name, args))
                    self.assertEqual(result.structured_content["error"]["code"], "invalid_param")

        asyncio.run(in_process())
        asyncio.run(stdio())


if __name__ == "__main__":
    unittest.main()
