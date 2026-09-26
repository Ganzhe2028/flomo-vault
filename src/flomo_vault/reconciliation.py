"""Deterministically reconcile local flomo identities with the Notion mirror."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit


MEMO_UID = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
FLOMO_HOSTS = {"v.flomoapp.com", "flomoapp.com", "www.flomoapp.com"}


def extract_memo_uid(link: Any) -> tuple[str | None, str | None]:
    if not isinstance(link, str) or not link.strip():
        return None, "blank_link"
    try:
        parts = urlsplit(link.strip())
    except ValueError:
        return None, "invalid_link"
    if parts.scheme not in {"http", "https"} or parts.hostname not in FLOMO_HOSTS:
        return None, "invalid_link"
    values = parse_qs(parts.query).get("memo_id", [])
    if len(values) != 1 or not MEMO_UID.fullmatch(values[0]):
        return None, "invalid_memo_id"
    return values[0], None


def normalize_input(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("rows", payload.get("results"))
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("输入应为 JSON 数组，或包含 rows/results 数组的对象")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            normalized.append({"id": None, "url": None, "created_at": None, "link": None})
            continue
        normalized.append(
            {
                "id": row.get("id") or row.get("page_id"),
                "url": row.get("url") or row.get("page_url"),
                "created_at": row.get("created_at") or row.get("date:Created At:start") or row.get("createdTime"),
                "link": row.get("link") if "link" in row else row.get("Link"),
            }
        )
    return normalized


def load_local_memos(vault_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current = vault_root / "current"
    manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
    memos = []
    with (current / "memos.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                memos.append(json.loads(line))
    return manifest, memos


def build_reconciliation(
    local_manifest: dict[str, Any],
    memos: list[dict[str, Any]],
    notion_rows: Iterable[dict[str, Any]],
    source: str,
    *,
    error: str | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    moment = now or datetime.now().astimezone()
    local_ids = {str(row.get("memo_uid")) for row in memos if row.get("memo_uid")}
    by_uid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_rows: list[dict[str, Any]] = []
    rows = list(notion_rows)
    if error is None:
        for row in rows:
            uid, reason = extract_memo_uid(row.get("link"))
            if reason:
                invalid_rows.append({"page_id": row.get("id"), "page_url": row.get("url"), "reason": reason})
            else:
                by_uid[str(uid)].append(row)
    duplicate_ids = sorted(uid for uid, matches in by_uid.items() if len(matches) > 1)
    mapping = []
    matched = 0
    pending = 0
    for memo in memos:
        uid = str(memo.get("memo_uid") or "")
        matches = by_uid.get(uid, [])
        if error is not None:
            status = "notion_unavailable"
            selected = None
        elif len(matches) > 1:
            status = "duplicate_notion"
            selected = None
        elif matches:
            status = "matched"
            selected = matches[0]
            matched += 1
        else:
            status = "pending_notion"
            selected = None
            pending += 1
        mapping.append(
            {
                "memo_uid": uid,
                "notion_page_id": selected.get("id") if selected else None,
                "notion_page_url": selected.get("url") if selected else None,
                "notion_created_at": selected.get("created_at") if selected else None,
                "match_status": status,
            }
        )
    notion_only = sorted(uid for uid in by_uid if uid not in local_ids)
    if error is not None:
        status = "unavailable"
    elif invalid_rows or duplicate_ids:
        status = "invalid"
    elif pending or notion_only:
        status = "pending"
    else:
        status = "complete"
    report = {
        "schema_version": 1,
        "run_id": f"{local_manifest.get('run_id', 'unknown')}-{moment.strftime('%Y%m%d-%H%M%S-%f')}",
        "created_at": moment.isoformat(),
        "local_run_id": local_manifest.get("run_id"),
        "source": source,
        "status": status,
        "error": error,
        "counts": {
            "local_memos": len(memos),
            "notion_rows": len(rows),
            "matched": matched,
            "pending_notion": pending,
            "notion_only": len(notion_only),
            "invalid_rows": len(invalid_rows),
            "duplicate_memo_ids": len(duplicate_ids),
        },
        "notion_only_memo_uids": notion_only,
        "duplicate_memo_uids": duplicate_ids,
        "invalid_rows": invalid_rows,
    }
    return report, mapping


def publish_reconciliation(
    vault_root: Path,
    report: dict[str, Any],
    mapping: list[dict[str, Any]],
    retention: int = 14,
) -> dict[str, Any]:
    root = vault_root / "reconciliation"
    snapshots = root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    run_id = str(report["run_id"])
    stage = snapshots / f".{run_id}.tmp"
    final = snapshots / run_id
    stage.mkdir()
    try:
        with (stage / "memo_notion_map.jsonl").open("w", encoding="utf-8") as handle:
            for row in mapping:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        (stage / "reconcile.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (stage / "RECONCILE.md").write_text(_markdown_report(report), encoding="utf-8")
        os.replace(stage, final)
        link_temp = root / f".current-{run_id}"
        link_temp.symlink_to(Path("snapshots") / run_id)
        os.replace(link_temp, root / "current")
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    completed = sorted(path for path in snapshots.iterdir() if path.is_dir() and not path.name.startswith("."))
    for old in completed[: max(0, len(completed) - max(1, retention))]:
        shutil.rmtree(old)
    return report


def _markdown_report(report: dict[str, Any]) -> str:
    counts = report["counts"]
    error = f"\n错误：{report['error']}\n" if report.get("error") else ""
    return f"""# flomo × Notion 校对报告

状态：{report['status']}

来源：{report['source']}
{error}
- 本地 memo：{counts['local_memos']}
- Notion 行：{counts['notion_rows']}
- 已对应：{counts['matched']}
- Notion 待追平：{counts['pending_notion']}
- 仅 Notion 存在：{counts['notion_only']}
- 非法或空 Link：{counts['invalid_rows']}
- 重复 memo_id：{counts['duplicate_memo_ids']}

Notion 仅用于只读校对；memo 关系仍以本地 Vault 为准。
"""
