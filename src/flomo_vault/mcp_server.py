"""Read-only MCP access to a published flomo vault."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from mcp.server import MCPServer


DEFAULT_VAULT = Path.home() / "Documents" / "Flomo Vault"
PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_CONTEXT_MEMOS = 50


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(row, dict):
                skipped += 1
                continue
            rows.append(row)
    return rows, skipped


@dataclass(frozen=True)
class Snapshot:
    path: Path
    manifest: dict[str, Any]
    memos: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    links: list[dict[str, Any]]
    warnings: list[str]


class VaultStore:
    """Keep the last complete load and follow the published current symlink."""

    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get("FLOMO_VAULT_ROOT")
        self.root = (root or (Path(configured).expanduser() if configured else DEFAULT_VAULT)).resolve()
        self._snapshot: Snapshot | None = None

    def _load(self) -> Snapshot:
        if not self.root.is_dir():
            raise VaultError("vault_not_found", f"Vault 不存在：{self.root}。请先运行 flomo-vault sync。")
        current = self.root / "current"
        try:
            path = current.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise VaultError("vault_empty", "Vault 没有可用的 current 快照。请先运行 flomo-vault sync。") from error
        if not path.is_dir():
            raise VaultError("vault_empty", "Vault 的 current 快照为空。请先运行 flomo-vault sync。")
        if not path.is_relative_to((self.root / "snapshots").resolve()):
            raise VaultError("vault_unreadable", "current 指向 vault 快照目录之外。请检查 vault 并重新同步。")
        manifest_path = path / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise VaultError("manifest_unreadable", "无法读取 manifest.json。请运行 flomo-vault doctor 并重新同步。") from error
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 2 or not manifest.get("run_id"):
            raise VaultError("manifest_unreadable", "manifest.json 的格式或版本无法识别。请重新运行 flomo-vault sync。")
        if (manifest.get("status") not in {"complete", "partial_media", "metadata_only"}
                or any(not isinstance(manifest.get(key), dict) for key in ("counts", "changes", "relations", "media"))):
            raise VaultError("manifest_unreadable", "manifest.json 缺少必要字段。请重新运行 flomo-vault sync。")
        if self._snapshot and self._snapshot.path == path and self._snapshot.manifest.get("run_id") == manifest["run_id"]:
            return self._snapshot
        try:
            memos, bad_memos = _read_jsonl(path / "memos.jsonl")
            attachments, bad_attachments = _read_jsonl(path / "attachments.jsonl")
            links, bad_links = _read_jsonl(path / "links.jsonl")
        except (OSError, UnicodeError) as error:
            raise VaultError("vault_unreadable", "快照文件无法读取。请运行 flomo-vault doctor 并重新同步。") from error
        counts = manifest.get("counts") or {}
        for name, rows, skipped in (("memos", memos, bad_memos), ("attachments", attachments, bad_attachments), ("links", links, bad_links)):
            expected = counts.get(name)
            if type(expected) is int and len(rows) + skipped != expected:
                raise VaultError("vault_unreadable", f"{name}.jsonl 行数与 manifest 不一致。请重新运行 flomo-vault sync。")
        if not memos:
            raise VaultError("vault_empty", "Vault 没有可读的 memo。请先运行 flomo-vault sync。")
        warnings = [
            f"{name}: skipped {count} malformed JSONL row(s)"
            for name, count in (("memos", bad_memos), ("attachments", bad_attachments), ("links", bad_links))
            if count
        ]
        snapshot = Snapshot(path, manifest, memos, attachments, links, warnings)
        self._snapshot = snapshot
        return snapshot

    def current(self) -> tuple[Snapshot | None, dict[str, Any]]:
        try:
            snapshot = self._load()
        except VaultError as error:
            if self._snapshot is None:
                return None, _error(error.code, error.message)
            return self._snapshot, {"ok": True, "stale": True, "warnings": [f"Using previous snapshot: {error.message}"]}
        result: dict[str, Any] = {"ok": True}
        if snapshot.warnings:
            result["warnings"] = list(snapshot.warnings)
        return snapshot, result


class VaultError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _page(limit: int, offset: int) -> dict[str, Any] | None:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE or type(offset) is not int or offset < 0:
        return _error("invalid_param", "limit 必须为 1–100，offset 必须为非负整数。")
    return None


def _dates(since: str | None, until: str | None) -> dict[str, Any] | None:
    try:
        for value in (since, until):
            if value is not None and (not isinstance(value, str) or date.fromisoformat(value).isoformat() != value):
                raise ValueError
    except ValueError:
        return _error("invalid_param", "日期必须使用 YYYY-MM-DD 格式。")
    if since and until and since > until:
        return _error("invalid_date_range", "since 不能晚于 until。请调整日期范围。")
    return None


def _summary(memo: dict[str, Any]) -> dict[str, Any]:
    return {key: memo.get(key) for key in ("memo_uid", "created_at", "tags", "body_markdown", "pin", "flomo_url")}


def _context_summary(memo: dict[str, Any]) -> dict[str, Any]:
    return {
        "memo_uid": memo.get("memo_uid"),
        "created_at": memo.get("created_at"),
        "body_markdown": str(memo.get("body_markdown") or "")[:500],
    }


def _sorted_memos(memos: list[dict[str, Any]], *, reverse: bool = True) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple[float, str]:
        try:
            created = datetime.fromisoformat(str(row.get("created_at") or ""))
            moment = created.timestamp()
        except (OSError, OverflowError, ValueError):
            moment = float("-inf")
        return moment, str(row.get("memo_uid") or "")

    return sorted(memos, key=key, reverse=reverse)


def _paginate(rows: list[dict[str, Any]], limit: int, offset: int, key: str) -> dict[str, Any]:
    return {key: rows[offset : offset + limit], "total": len(rows), "has_more": offset + limit < len(rows), "limit": limit, "offset": offset}


def _visible_memos(snapshot: Snapshot, include_deleted: bool) -> list[dict[str, Any]]:
    return [row for row in snapshot.memos if include_deleted or not row.get("is_deleted")]


def _filtered_memos(
    snapshot: Snapshot, since: str | None, until: str | None, tag: str | None, include_deleted: bool
) -> list[dict[str, Any]]:
    rows = _visible_memos(snapshot, include_deleted)
    if since:
        rows = [row for row in rows if str(row.get("created_at") or "")[:10] >= since]
    if until:
        rows = [row for row in rows if str(row.get("created_at") or "")[:10] <= until]
    if tag is not None:
        rows = [row for row in rows if tag in (row.get("tags") or [])]
    return rows


class VaultTools:
    def __init__(self, store: VaultStore | None = None, *, today: date | None = None) -> None:
        self.store = store or VaultStore()
        self.today = today

    def get_vault_status(self) -> dict[str, Any]:
        """Check the current snapshot's freshness, selection, counts, and media status first."""
        snapshot, base = self.store.current()
        if snapshot is None:
            return base
        manifest = snapshot.manifest
        result = {**base, "vault_root": str(self.store.root)}
        result.update({key: manifest.get(key) for key in ("run_id", "created_at", "status", "counts", "changes", "relations", "selection")})
        if manifest.get("status") != "complete":
            media = manifest.get("media") or {}
            result["warning"] = f"{manifest.get('status')}: {media.get('failed', 0)} attachments failed, {media.get('deferred', 0)} deferred"
        if manifest.get("selection"):
            result.setdefault("warnings", []).append("This vault contains a bounded selection; counts may not describe all flomo memos.")
        return result

    def search_memos(
        self, query: str, since: str | None = None, until: str | None = None, tag: str | None = None,
        include_deleted: bool = False, limit: int = PAGE_SIZE, offset: int = 0,
    ) -> dict[str, Any]:
        """Find memos by case-insensitive AND terms in plain text, with optional date and tag filters."""
        problem = _page(limit, offset) or _dates(since, until)
        if problem:
            return problem
        if not isinstance(query, str) or not query.split():
            return _error("invalid_param", "query 不能为空。请输入要搜索的词。")
        snapshot, base = self.store.current()
        if snapshot is None:
            return base
        words = [word.casefold() for word in query.split()]
        rows = [row for row in _filtered_memos(snapshot, since, until, tag, include_deleted)
                if all(word in str(row.get("body_text") or "").casefold() for word in words)]
        return {**base, **_paginate([_summary(row) for row in _sorted_memos(rows)], limit, offset, "memos")}

    def list_memos(
        self, since: str | None = None, until: str | None = None, tag: str | None = None,
        include_deleted: bool = False, limit: int = PAGE_SIZE, offset: int = 0, order: str = "desc",
    ) -> dict[str, Any]:
        """Browse memo summaries by creation time, date range, and exact tag."""
        problem = _page(limit, offset) or _dates(since, until)
        if problem:
            return problem
        if order not in {"asc", "desc"}:
            return _error("invalid_param", "order 只能是 asc 或 desc。")
        snapshot, base = self.store.current()
        if snapshot is None:
            return base
        rows = _sorted_memos(_filtered_memos(snapshot, since, until, tag, include_deleted), reverse=order == "desc")
        return {**base, **_paginate([_summary(row) for row in rows], limit, offset, "memos")}

    def get_memo(self, memo_uid: str, include_deleted: bool = False) -> dict[str, Any]:
        """Read one full memo and its attachment metadata, local paths, and transcripts."""
        if not isinstance(memo_uid, str) or not memo_uid:
            return _error("invalid_param", "memo_uid 不能为空。")
        snapshot, base = self.store.current()
        if snapshot is None:
            return base
        memo = next((row for row in _visible_memos(snapshot, include_deleted) if row.get("memo_uid") == memo_uid), None)
        if memo is None:
            return _error("memo_not_found", "找不到这条 memo，或它已删除。请检查 memo_uid；读取已删除 memo 可传 include_deleted: true。")
        attachments = []
        for row in snapshot.attachments:
            if row.get("memo_slug") != memo.get("slug"):
                continue
            item = {key: row.get(key) for key in ("attachment_id", "type", "name", "media_relpath", "download_status", "transcript")}
            relative = row.get("media_relpath")
            local_path = None
            if isinstance(relative, str) and relative:
                candidate = (self.store.root / relative).resolve()
                media_root = (self.store.root / "media").resolve()
                if candidate.is_relative_to(media_root):
                    local_path = str(candidate)
            item["local_path"] = local_path
            attachments.append(item)
        return {**base, "memo": {**memo, "attachments": attachments}}

    def get_memo_context(self, memo_uid: str, depth: int = 1, include_deleted: bool = False) -> dict[str, Any]:
        """Read a memo and its directed references; depth 2 also lists unique second-hop neighbors."""
        if not isinstance(memo_uid, str) or not memo_uid or type(depth) is not int or depth not in (1, 2):
            return _error("invalid_param", "memo_uid 不能为空，depth 只能是 1 或 2。")
        snapshot, base = self.store.current()
        if snapshot is None:
            return base
        all_memos = {str(row.get("memo_uid")): row for row in snapshot.memos}
        center = all_memos.get(memo_uid)
        if center is None or (center.get("is_deleted") and not include_deleted):
            return _error("memo_not_found", "找不到这条 memo，或它已删除。读取已删除中心 memo 可传 include_deleted: true。")
        active = {uid: row for uid, row in all_memos.items() if not row.get("is_deleted")}
        groups = self._neighbors(snapshot.links, memo_uid, active)
        selected: set[str] = set()
        output: dict[str, list[dict[str, Any]]] = {key: [] for key in ("outgoing", "incoming", "bidirectional")}
        truncated = False
        for key in output:
            for uid in sorted(groups[key]):
                if uid == memo_uid or uid in selected:
                    continue
                if len(selected) >= MAX_CONTEXT_MEMOS:
                    truncated = True
                    break
                selected.add(uid)
                output[key].append(_context_summary(active[uid]))
        second_hop: list[dict[str, Any]] = []
        if depth == 2:
            found: dict[str, dict[str, Any]] = {}
            for via in sorted(selected):
                for direction, neighbors in self._neighbors(snapshot.links, via, active).items():
                    for uid in sorted(neighbors):
                        if uid == memo_uid or uid in selected:
                            continue
                        item = found.setdefault(uid, {**_context_summary(active[uid]), "via": [], "directions": []})
                        if via not in item["via"]:
                            item["via"].append(via)
                        if direction not in item["directions"]:
                            item["directions"].append(direction)
            for uid in sorted(found):
                if len(selected) + len(second_hop) >= MAX_CONTEXT_MEMOS:
                    truncated = True
                    break
                second_hop.append(found[uid])
        return {**base, "center": center, **output, "second_hop": second_hop, "truncated": truncated}

    @staticmethod
    def _neighbors(links: list[dict[str, Any]], uid: str, active: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
        outgoing = {str(row.get("target_memo_uid")) for row in links
                    if row.get("kind") == "memo" and row.get("source_memo_uid") == uid and str(row.get("target_memo_uid")) in active}
        incoming = {str(row.get("source_memo_uid")) for row in links
                    if row.get("kind") == "memo" and row.get("target_memo_uid") == uid and str(row.get("source_memo_uid")) in active}
        mutual = outgoing & incoming
        return {"outgoing": outgoing - mutual, "incoming": incoming - mutual, "bidirectional": mutual}

    def list_tags(self) -> dict[str, Any]:
        """Count tags across active memos in the current snapshot."""
        snapshot, base = self.store.current()
        if snapshot is None:
            return base
        counts: Counter[str] = Counter()
        for memo in _visible_memos(snapshot, False):
            counts.update(set(str(tag) for tag in (memo.get("tags") or [])))
        return {**base, "tags": [{"name": tag, "memo_count": count} for tag, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))]}

    def get_stats(self) -> dict[str, Any]:
        """Summarize active memos by month, hour, tag, directed links, and attachments."""
        snapshot, base = self.store.current()
        if snapshot is None:
            return base
        active = _visible_memos(snapshot, False)
        active_ids = {str(row.get("memo_uid")) for row in active}
        today = self.today or datetime.now().astimezone().date()
        months = []
        for ago in range(23, -1, -1):
            month_index = today.year * 12 + today.month - 1 - ago
            months.append(f"{month_index // 12:04d}-{month_index % 12 + 1:02d}")
        month_counts = Counter(str(row.get("created_at") or "")[:7] for row in active)
        hour_counts = Counter(str(row.get("created_at") or "")[11:13] for row in active)
        tags: Counter[str] = Counter()
        for row in active:
            tags.update(set(str(tag) for tag in (row.get("tags") or [])))
        active_links = [row for row in snapshot.links
                        if row.get("kind") == "memo" and str(row.get("source_memo_uid")) in active_ids
                        and str(row.get("target_memo_uid")) in active_ids]
        edges = {(str(row.get("source_memo_uid")), str(row.get("target_memo_uid"))) for row in active_links}
        pairs = {tuple(sorted((source, target))) for source, target in edges if source != target and (target, source) in edges}
        external = sum(row.get("kind") == "external" and str(row.get("source_memo_uid")) in active_ids for row in snapshot.links)
        with_attachments = sum(bool(row.get("attachment_ids")) for row in active)
        result = {
            **base,
            "active_memos": len(active),
            "monthly_memos": [{"month": month, "count": month_counts[month]} for month in months],
            "hourly_memos": [{"hour": hour, "count": hour_counts[f"{hour:02d}"]} for hour in range(24)],
            "top_tags": [{"name": tag, "memo_count": count} for tag, count in sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))[:20]],
            "relations": {"memo_edges": len(active_links), "bidirectional_pairs": len(pairs), "external_links": external},
            "memos_with_attachments": with_attachments,
            "attachment_memo_ratio": with_attachments / len(active) if active else 0.0,
        }
        if snapshot.manifest.get("selection"):
            result.setdefault("warnings", []).append("Stats cover only the selected vault snapshot, not all flomo memos.")
        return result


def create_server(store: VaultStore | None = None) -> MCPServer:
    tools = VaultTools(store)
    server = MCPServer(
        "flomo-vault",
        instructions="Read-only local flomo vault. Call get_vault_status first to check freshness and selection. "
        "Search and browse active memos with the other tools; pass include_deleted=true only when requested. "
        "No tool edits memos or triggers sync.",
    )
    for name in ("get_vault_status", "search_memos", "get_memo", "get_memo_context", "list_memos", "list_tags", "get_stats"):
        server.tool()(getattr(tools, name))
    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
