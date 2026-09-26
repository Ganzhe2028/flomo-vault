"""Write an atomic, versioned flomo vault."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .database import VaultData


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def _read_previous_memos(vault_root: Path) -> dict[str, dict[str, Any]]:
    path = vault_root / "current" / "memos.jsonl"
    if not path.is_file():
        return {}
    result = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                result[str(row["memo_uid"])] = row
    except (OSError, ValueError, KeyError):
        return {}
    return result


def _memo_signature(row: dict[str, Any]) -> str:
    selected = {
        key: row.get(key)
        for key in ("content_html", "updated_at", "deleted_at", "is_deleted", "tags", "attachment_ids", "pin")
    }
    return hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _changes(previous: dict[str, dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, int]:
    current_map = {str(row["memo_uid"]): row for row in current}
    added = set(current_map) - set(previous)
    removed = set(previous) - set(current_map)
    common = set(current_map) & set(previous)
    changed = {uid for uid in common if _memo_signature(current_map[uid]) != _memo_signature(previous[uid])}
    return {"added": len(added), "updated": len(changed), "removed_from_database": len(removed), "unchanged": len(common - changed)}


def _write_monthly(path: Path, memos: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> int:
    attachment_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attachment in attachments:
        attachment_map[str(attachment.get("memo_slug"))].append(attachment)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for memo in memos:
        if memo.get("is_deleted"):
            continue
        created = str(memo.get("created_at") or "unknown")
        month = created[:7] if len(created) >= 7 else "unknown"
        groups[month].append(memo)
    path.mkdir(parents=True, exist_ok=True)
    for month, rows in sorted(groups.items()):
        lines = [f"# flomo {month}", "", "> 由本地数据库重建，不是 flomo 官方桌面导出文件。", ""]
        for memo in rows:
            created = memo.get("created_at") or "时间未知"
            tags = " ".join(f"#{tag}" for tag in memo.get("tags", []))
            lines.extend([f"## {created}", ""])
            if tags:
                lines.extend([tags, ""])
            lines.extend([memo.get("body_markdown") or "", ""])
            for attachment in sorted(attachment_map.get(str(memo.get("slug")), []), key=lambda row: row.get("index") or 0):
                relative = attachment.get("media_relpath")
                if not relative:
                    continue
                label = attachment.get("name") or attachment.get("type") or "附件"
                lines.append(f"- [{label}](../../../{relative})")
            lines.extend(["", "---", ""])
        (path / f"{month}.md").write_text("\n".join(lines), encoding="utf-8")
    return len(groups)


def _report(manifest: dict[str, Any]) -> str:
    counts = manifest["counts"]
    changes = manifest["changes"]
    media = manifest["media"]
    relations = manifest["relations"]
    selection = manifest.get("selection") or {}
    scope = "全部本地笔记"
    if selection:
        scope = f"创建日期 {selection.get('since') or '不限'} 至 {selection.get('until') or '现在'}；附件类型 {', '.join(selection.get('attachment_types') or ['全部'])}"
    return f"""# flomo 本地资料库同步报告

状态：{manifest['status']}

范围：{scope}

本次从 flomo 本地数据库重建，**不是 flomo 官方桌面导出文件**。

- 笔记：{counts['memos']} 条（有效 {counts['active_memos']}，已删除 {counts['deleted_memos']}）
- 附件记录：{counts['attachments']} 条
- 历史版本：{counts['histories']} 条
- 标签：{counts['tags']} 条
- 草稿：{counts['drafts']} 条
- Memo 引用：单向边 {relations['one_way_edges']}，双向关系 {relations['bidirectional_pairs']} 对，外部链接 {relations['external_links']}
- 与上次相比：新增 {changes['added']}，更新 {changes['updated']}，数据库中消失 {changes['removed_from_database']}
- 附件文件：新获得 {media['downloaded']}（本机缓存恢复 {media.get('recovered', 0)}），沿用 {media['existing']}，失败 {media['failed']}，本次未处理 {media['deferred']}

机器读取入口：`current/memos.jsonl`。按月阅读入口：`current/monthly/`。
"""


def publish_snapshot(
    vault_root: Path,
    data: VaultData,
    media_stats: dict[str, int],
    retention: int = 7,
    source_label: str = "local-app-snapshot",
    now: datetime | None = None,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    vault_root.mkdir(parents=True, exist_ok=True)
    (vault_root / "media").mkdir(exist_ok=True)
    snapshots = vault_root / "snapshots"
    snapshots.mkdir(exist_ok=True)
    previous = _read_previous_memos(vault_root)

    moment = now or datetime.now().astimezone()
    run_id = moment.strftime("%Y%m%d-%H%M%S-%f")
    stage = snapshots / f".{run_id}.tmp"
    final = snapshots / run_id
    stage.mkdir()
    try:
        _write_jsonl(stage / "memos.jsonl", data.memos)
        _write_jsonl(stage / "attachments.jsonl", data.attachments)
        _write_jsonl(stage / "history.jsonl", data.histories)
        _write_jsonl(stage / "tags.jsonl", data.tags)
        _write_jsonl(stage / "links.jsonl", data.links)
        _write_jsonl(stage / "drafts.jsonl", data.drafts)
        month_count = _write_monthly(stage / "monthly", data.memos, data.attachments)
        deleted_memos = sum(bool(row.get("is_deleted")) for row in data.memos)
        if media_stats["failed"]:
            status = "partial_media"
        elif media_stats["deferred"]:
            status = "metadata_only"
        else:
            status = "complete"
        manifest: dict[str, Any] = {
            "schema_version": 2,
            "tool_version": __version__,
            "run_id": run_id,
            "created_at": moment.isoformat(),
            "source": source_label,
            "status": status,
            "selection": selection or {},
            "counts": {
                "memos": len(data.memos),
                "active_memos": len(data.memos) - deleted_memos,
                "deleted_memos": deleted_memos,
                "attachments": len(data.attachments),
                "histories": len(data.histories),
                "tags": len(data.tags),
                "links": len(data.links),
                "drafts": len(data.drafts),
                "months": month_count,
            },
            "changes": _changes(previous, data.memos),
            "media": media_stats,
            "relations": {
                "memo_edges": sum(row.get("kind") == "memo" for row in data.links),
                "one_way_edges": sum(row.get("kind") == "memo" and not row.get("is_bidirectional") for row in data.links),
                "bidirectional_directed_edges": sum(row.get("kind") == "memo" and bool(row.get("is_bidirectional")) for row in data.links),
                "bidirectional_pairs": sum(row.get("kind") == "memo" and bool(row.get("is_bidirectional")) for row in data.links) // 2,
                "external_links": sum(row.get("kind") == "external" for row in data.links),
            },
            "auxiliary_counts": data.auxiliary_counts,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (stage / "REPORT.md").write_text(_report(manifest), encoding="utf-8")
        os.replace(stage, final)

        link_temp = vault_root / f".current-{run_id}"
        link_temp.symlink_to(Path("snapshots") / run_id)
        os.replace(link_temp, vault_root / "current")
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    completed = sorted(path for path in snapshots.iterdir() if path.is_dir() and not path.name.startswith("."))
    for old in completed[: max(0, len(completed) - max(1, retention))]:
        shutil.rmtree(old)
    return manifest
