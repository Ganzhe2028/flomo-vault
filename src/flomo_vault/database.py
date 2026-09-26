"""Parse flomo's Chromium IndexedDB snapshot into stable Python records."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import html
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .compat import install_snappy_shim
from .htmltext import html_to_markdown, html_to_text
from .local_storage import read_drafts

install_snappy_shim()

from dfindexeddb.indexeddb import types  # noqa: E402
from dfindexeddb.indexeddb.chromium import record  # noqa: E402
from dfindexeddb.leveldb import definitions  # noqa: E402


@dataclass(slots=True)
class VaultData:
    memos: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    histories: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    links: list[dict[str, Any]]
    drafts: list[dict[str, Any]]
    auxiliary_counts: dict[str, int]


def select_data(
    data: VaultData,
    since: str | None = None,
    until: str | None = None,
    attachment_types: set[str] | None = None,
) -> VaultData:
    """Return a coherent date/type-bounded view without mutating parsed data."""
    def included(memo: dict[str, Any]) -> bool:
        created = str(memo.get("created_at") or "")[:10]
        if since and (not created or created < since):
            return False
        if until and (not created or created > until):
            return False
        return True

    selected_memos = [dict(memo) for memo in data.memos if included(memo)]
    slugs = {str(memo.get("slug")) for memo in selected_memos if memo.get("slug")}
    selected_attachments = [
        dict(row)
        for row in data.attachments
        if str(row.get("memo_slug")) in slugs
        and (attachment_types is None or str(row.get("type")) in attachment_types)
    ]
    retained_ids = {row["attachment_id"] for row in selected_attachments}
    for memo in selected_memos:
        memo["attachment_ids"] = [value for value in memo.get("attachment_ids", []) if value in retained_ids]
    selected_histories = [dict(row) for row in data.histories if str(row.get("memo_slug")) in slugs]
    selected_drafts = [dict(row) for row in data.drafts if str(row.get("memo_slug")) in slugs]
    used_tags = {tag for memo in selected_memos for tag in memo.get("tags", [])}
    selected_tags = [dict(row) for row in data.tags if str(row.get("name")) in used_tags]
    selected_links = [
        dict(row)
        for row in data.links
        if str(row.get("source_memo_uid")) in slugs
        or (row.get("kind") == "memo" and str(row.get("target_memo_uid")) in slugs)
    ]
    auxiliary = dict(data.auxiliary_counts)
    auxiliary.update(
        {
            "selection_source_memos": len(data.memos),
            "selection_source_attachments": len(data.attachments),
        }
    )
    return VaultData(
        selected_memos,
        selected_attachments,
        selected_histories,
        selected_tags,
        selected_links,
        selected_drafts,
        auxiliary,
    )


def _unwrap(value: Any) -> Any:
    return getattr(value, "value", value)


def normalize(value: Any) -> Any:
    """Convert dfindexeddb JavaScript wrapper values to JSON-safe values."""
    value = _unwrap(value)
    if value is None or isinstance(value, (types.Null, types.Undefined)):
        return None
    if isinstance(value, types.JSArray):
        items = list(value.values)
        for key, item in value.properties.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if index >= len(items):
                items.extend([None] * (index + 1 - len(items)))
            items[index] = item
        return [normalize(item) for item in items if not isinstance(item, types.Undefined)]
    if isinstance(value, types.JSSet):
        return [normalize(item) for item in value.values]
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat(sep=" ")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _present(value: Any) -> bool:
    return value not in (None, "", 0, False)


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _date_value(row: dict[str, Any], name: str) -> str | None:
    value = row.get(name)
    if isinstance(value, str) and value:
        return value
    long_value = row.get(f"{name}_long")
    if isinstance(long_value, (int, float)) and long_value:
        seconds = long_value / 1000 if long_value > 10_000_000_000 else long_value
        try:
            return datetime.fromtimestamp(seconds).isoformat(sep=" ")
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _read_stores(database_dir: Path) -> tuple[dict[str, list[Any]], dict[str, int]]:
    raw: list[Any] = []
    store_names: dict[tuple[int, int], str] = {}
    reader = record.FolderReader(database_dir)
    for item in reader.GetRecords(
        use_manifest=True,
        use_sequence_number=True,
        load_blobs=False,
    ):
        if item.recovered is not False:
            continue
        if isinstance(item.key, record.ObjectStoreNamesKey):
            store_names[(int(item.database_id), int(item.value))] = item.key.object_store_name
        elif isinstance(item.key, record.ObjectStoreDataKey):
            raw.append(item)

    database_ids = {
        database_id
        for (database_id, _), name in store_names.items()
        if name == "memos"
    }
    if not database_ids:
        raise RuntimeError("没有在快照中找到 flomo memos 数据表")
    database_id = max(database_ids)

    rows: dict[str, list[Any]] = defaultdict(list)
    deleted_counts: dict[str, int] = defaultdict(int)
    for item in raw:
        if int(item.database_id) != database_id:
            continue
        name = store_names.get((database_id, int(item.object_store_id)))
        if not name:
            continue
        if item.type == definitions.InternalRecordType.DELETED:
            deleted_counts[name] += 1
            continue
        rows[name].append(normalize(item.value))
    return dict(rows), dict(deleted_counts)


def _memo_record(row: dict[str, Any]) -> dict[str, Any]:
    content = row.get("content") or ""
    files = row.get("files") if isinstance(row.get("files"), list) else []
    tags = row.get("tags") if isinstance(row.get("tags"), list) else []
    deleted = _present(row.get("deleted_at")) or _present(row.get("deleted_at_long"))
    slug = str(row.get("slug") or row.get("id") or "")
    return {
        "memo_uid": slug,
        "slug": row.get("slug"),
        "flomo_uid": slug,
        "flomo_numeric_id": row.get("id"),
        "flomo_url": f"https://v.flomoapp.com/mine/?memo_id={slug}" if slug else None,
        "content_html": content,
        "body_text": html_to_text(content),
        "body_markdown": html_to_markdown(content),
        "created_at": _date_value(row, "created_at"),
        "updated_at": _date_value(row, "updated_at"),
        "deleted_at": _date_value(row, "deleted_at"),
        "is_deleted": deleted,
        "pin": bool(row.get("pin")),
        "source": row.get("source"),
        "memo_from": row.get("memo_from"),
        "tags": [str(value) for value in tags if value is not None],
        "external_links": [],
        "outgoing_memo_uids": [],
        "incoming_memo_uids": [],
        "bidirectional_memo_uids": [],
        "attachment_ids": [str(value.get("id")) for value in files if isinstance(value, dict) and value.get("id") is not None],
        "is_show_cloud": bool(row.get("is_show_cloud")),
    }


def _target_memo_uid(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    decoded = html.unescape(value)
    parts = urlsplit(decoded)
    if parts.scheme in {"http", "https"}:
        if parts.hostname in {"v.flomoapp.com", "flomoapp.com", "www.flomoapp.com"}:
            memo_ids = parse_qs(parts.query).get("memo_id", [])
            return memo_ids[0] if memo_ids else None
        return None
    return decoded


def _build_links(rows: list[Any], memos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    internal_edges: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        raw_target = row.get("link")
        if not source or not isinstance(raw_target, str) or not raw_target:
            continue
        target_uid = _target_memo_uid(raw_target)
        if target_uid:
            internal_edges.add((source, target_uid))
            links.append(
                {
                    "link_id": row.get("id"),
                    "kind": "memo",
                    "source_memo_uid": source,
                    "target_memo_uid": target_uid,
                    "target_url": f"https://v.flomoapp.com/mine/?memo_id={target_uid}",
                }
            )
        else:
            links.append(
                {
                    "link_id": row.get("id"),
                    "kind": "external",
                    "source_memo_uid": source,
                    "target_memo_uid": None,
                    "target_url": html.unescape(raw_target),
                }
            )

    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    external: dict[str, set[str]] = defaultdict(set)
    for link in links:
        source = str(link["source_memo_uid"])
        target = link.get("target_memo_uid")
        if target:
            target = str(target)
            outgoing[source].add(target)
            incoming[target].add(source)
            link["is_bidirectional"] = (target, source) in internal_edges
        else:
            external[source].add(str(link["target_url"]))
            link["is_bidirectional"] = False
    for memo in memos:
        uid = str(memo["memo_uid"])
        memo["outgoing_memo_uids"] = sorted(outgoing[uid])
        memo["incoming_memo_uids"] = sorted(incoming[uid])
        memo["bidirectional_memo_uids"] = sorted(outgoing[uid] & incoming[uid])
        memo["external_links"] = sorted(external[uid])
    links.sort(
        key=lambda row: (
            str(row.get("source_memo_uid") or ""),
            str(row.get("target_memo_uid") or row.get("target_url") or ""),
        )
    )
    return links


def _attachment_record(
    embedded: dict[str, Any],
    stored: dict[str, Any],
    memo: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    merged = {**stored, **embedded}
    attachment_id = merged.get("id") or merged.get("uid")
    return {
        "attachment_id": str(attachment_id) if attachment_id is not None else f"{memo['slug']}:{index}",
        "memo_slug": memo.get("slug"),
        "index": merged.get("index") if merged.get("index") is not None else index,
        "type": merged.get("type") or "unknown",
        "name": merged.get("name"),
        "path": merged.get("path"),
        "size": merged.get("size"),
        "seconds": merged.get("seconds"),
        "transcript": merged.get("content"),
        "remote_url": _clean_url(merged.get("url")),
        "thumbnail_url": _clean_url(merged.get("thumbnail_url")),
        "memo_created_at": _date_value(memo, "created_at"),
        "media_relpath": None,
        "download_status": "not_requested",
    }


def parse_snapshot(snapshot_root: Path) -> VaultData:
    database_dir = snapshot_root / "IndexedDB" / "flomo_._0.indexeddb.leveldb"
    if not database_dir.is_dir():
        raise FileNotFoundError(f"IndexedDB 快照不存在：{database_dir}")
    rows, deleted_counts = _read_stores(database_dir)

    raw_memos = [row for row in rows.get("memos", []) if isinstance(row, dict)]
    memos = [_memo_record(row) for row in raw_memos]
    memos.sort(key=lambda row: (row.get("created_at") or "", row["memo_uid"]))

    stored_files = {
        str(row.get("id") or row.get("uid")): row
        for row in rows.get("files", [])
        if isinstance(row, dict) and (row.get("id") is not None or row.get("uid") is not None)
    }
    attachments: list[dict[str, Any]] = []
    for raw_memo in raw_memos:
        embedded_files = raw_memo.get("files") if isinstance(raw_memo.get("files"), list) else []
        for index, embedded in enumerate(embedded_files):
            if not isinstance(embedded, dict):
                continue
            key = str(embedded.get("id") or embedded.get("uid") or "")
            attachments.append(_attachment_record(embedded, stored_files.get(key, {}), raw_memo, index))
    attachments.sort(key=lambda row: (row.get("memo_created_at") or "", row["memo_slug"] or "", row["index"]))

    histories = []
    for row in rows.get("history", []):
        if not isinstance(row, dict):
            continue
        content = row.get("content") or ""
        histories.append(
            {
                "memo_slug": row.get("slug"),
                "timestamp": row.get("timestamp"),
                "content_html": content,
                "body_text": html_to_text(content),
            }
        )
    histories.sort(key=lambda row: (str(row.get("memo_slug") or ""), row.get("timestamp") or 0))

    tags = [row for row in rows.get("tags", []) if isinstance(row, dict)]
    links = _build_links(rows.get("links", []), memos)
    drafts = read_drafts(snapshot_root)
    for draft in drafts:
        draft["body_text"] = html_to_text(draft.get("content_html"))
        draft["body_markdown"] = html_to_markdown(draft.get("content_html"))

    auxiliary_counts = {name: len(values) for name, values in rows.items()}
    auxiliary_counts.update({f"{name}_tombstones": count for name, count in deleted_counts.items()})
    return VaultData(memos, attachments, histories, tags, links, drafts, auxiliary_counts)
