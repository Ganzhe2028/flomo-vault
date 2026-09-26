"""Read selected Chromium Local Storage values without exposing credentials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .compat import install_snappy_shim

install_snappy_shim()

from dfindexeddb.leveldb import definitions, record  # noqa: E402


def decode_chromium_string(data: bytes) -> str:
    if not data:
        return ""
    encoding = data[0]
    payload = data[1:]
    if encoding == 1:
        return payload.decode("latin-1")
    if encoding == 0:
        return payload.decode("utf-16-le")
    raise ValueError(f"Unsupported Chromium string encoding: {encoding}")


def decode_local_storage_key(raw_key: bytes) -> str | None:
    if not raw_key.startswith(b"_"):
        return None
    separator = raw_key.find(b"\x00", 1)
    if separator < 0:
        return None
    try:
        return decode_chromium_string(raw_key[separator + 1 :])
    except (UnicodeDecodeError, ValueError):
        return None


def read_values(leveldb_dir: Path, prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    result: dict[str, str] = {}
    reader = record.FolderReader(leveldb_dir)
    for item in reader.GetRecords(use_manifest=True, use_sequence_number=True):
        if item.recovered is not False:
            continue
        if item.record.record_type != definitions.InternalRecordType.VALUE:
            continue
        key = decode_local_storage_key(item.record.key)
        if key is None or (prefixes and not key.startswith(prefixes)):
            continue
        try:
            result[key] = decode_chromium_string(item.record.value)
        except (UnicodeDecodeError, ValueError):
            continue
    return result


def read_access_token(snapshot_root: Path) -> str | None:
    values = read_values(snapshot_root / "Local Storage" / "leveldb", ("me",))
    raw = values.get("me")
    if not raw:
        return None
    try:
        me = json.loads(raw)
    except json.JSONDecodeError:
        return None
    token = me.get("access_token") if isinstance(me, dict) else None
    return token if isinstance(token, str) and token else None


def read_drafts(snapshot_root: Path) -> list[dict[str, Any]]:
    values = read_values(
        snapshot_root / "Local Storage" / "leveldb",
        ("draft:edit:", "draft:annotate:"),
    )
    drafts = []
    for key, content in sorted(values.items()):
        parts = key.split(":", 2)
        if len(parts) != 3:
            continue
        drafts.append(
            {
                "draft_key": key,
                "kind": parts[1],
                "memo_slug": parts[2],
                "content_html": content,
            }
        )
    return drafts
