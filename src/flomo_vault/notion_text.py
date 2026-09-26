"""Incrementally publish plain-text memo pages under the separate Notion archive."""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from zoneinfo import ZoneInfo

from .notion import NOTION_API_BASE, NOTION_API_VERSION, _retry_delay, system_trust_urlopen
from .config import setting


ROOT_PAGE_ID = setting("notion_text_root_page_id")
ARCHIVE_SINCE = setting("archive_since", "1970-01-01")
KEYCHAIN_SERVICE = "flomo-local-vault.notion-text"
KEYCHAIN_ACCOUNT = ROOT_PAGE_ID


class TextArchiveError(RuntimeError):
    pass


def read_text_token() -> str | None:
    if not KEYCHAIN_ACCOUNT:
        return None
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def store_text_token(token: str) -> None:
    if not KEYCHAIN_ACCOUNT:
        raise TextArchiveError("Set notion_text_root_page_id in ~/.config/flomo-vault/config.json first")
    if not token.strip():
        raise TextArchiveError("文字归档 Token 为空")
    result = subprocess.run(
        ["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w", token.strip()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode:
        raise TextArchiveError("无法保存文字归档 Token 到 Keychain")


class TextArchiveClient:
    def __init__(self, token: str, *, opener: Callable[..., Any] = system_trust_urlopen, sleeper: Callable[[float], None] = time.sleep):
        self.token = token
        self.opener = opener
        self.sleeper = sleeper
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0

    def _pace(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request_at)
            self._next_request_at = scheduled + 0.36
        if scheduled > now:
            self.sleeper(scheduled - now)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(
            NOTION_API_BASE + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_API_VERSION, "Content-Type": "application/json"},
        )
        # Reads are safe to retry. A timed-out write might have succeeded: callers
        # must discover the page before trying to create it again.
        attempts = 3 if method in {"GET", "PATCH"} else 1
        for attempt in range(attempts):
            try:
                # Share one ~2.8 req/s budget across concurrent page writes.
                self._pace()
                with self.opener(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                if error.code in {401, 403, 404}:
                    raise TextArchiveError("文字归档凭证无效或根页面未分享给写入 connection") from None
                if (error.code == 429 or 500 <= error.code < 600) and attempt + 1 < attempts:
                    self.sleeper(_retry_delay(error.headers, attempt))
                    continue
                if error.code == 429 and method == "POST":
                    self.sleeper(_retry_delay(error.headers, attempt))
                raise TextArchiveError(f"Notion 文字归档请求失败（HTTP {error.code}）") from None
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                if attempt + 1 < attempts:
                    self.sleeper(float(2**attempt))
                    continue
                raise TextArchiveError(f"Notion 文字归档请求未确认：{type(error).__name__}") from None
        raise TextArchiveError("Notion 文字归档请求失败")

    def validate_root(self) -> None:
        if not ROOT_PAGE_ID:
            raise TextArchiveError("Set notion_text_root_page_id in ~/.config/flomo-vault/config.json first")
        page = self.request("GET", f"/pages/{ROOT_PAGE_ID}")
        if page.get("id", "").replace("-", "") != ROOT_PAGE_ID.replace("-", ""):
            raise TextArchiveError("文字归档根页面 ID 不匹配")

    def children(self, parent_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            page = self.request("GET", f"/blocks/{parent_id}/children?{urlencode(query)}")
            values = page.get("results")
            if not isinstance(values, list):
                raise TextArchiveError("Notion 子页面列表无效")
            results.extend(v for v in values if isinstance(v, dict) and v.get("type") == "child_page")
            if not page.get("has_more"):
                return results
            cursor = page.get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                raise TextArchiveError("Notion 子页面分页缺少游标")

    def find_child(self, parent_id: str, title: str) -> str | None:
        matches = [v.get("id") for v in self.children(parent_id) if v.get("child_page", {}).get("title") == title]
        if len(matches) > 1:
            raise TextArchiveError(f"Notion 层级存在同名页面：{title}")
        return matches[0] if matches else None

    def create(
        self, parent_id: str, title: str, content: str | None = None,
        fallback_blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_id},
            "properties": {"title": {"type": "title", "title": [{"type": "text", "text": {"content": title}}]}},
        }
        if content is not None:
            body["markdown"] = content
        try:
            return self.request("POST", "/pages", body)
        except TextArchiveError as error:
            if "HTTP 400" not in str(error) or fallback_blocks is None:
                raise
            # Some valid plain text cannot be parsed by Notion's Markdown
            # converter. Structured rich text preserves it literally.
            body.pop("markdown", None)
            body["children"] = fallback_blocks
            return self.request("POST", "/pages", body)

    def create_or_find(
        self, parent_id: str, title: str, content: str | None = None,
        fallback_blocks: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str | None]:
        for attempt in range(3):
            existing = self.find_child(parent_id, title)
            if existing:
                return existing, None
            try:
                page = self.create(parent_id, title, content, fallback_blocks)
                return page["id"], page.get("url")
            except TextArchiveError as error:
                # Never repeat an ambiguous create until the parent has been
                # re-read: the first request may have committed.
                existing = self.find_child(parent_id, title)
                if existing:
                    return existing, None
                retryable = any(code in str(error) for code in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "HTTP 529", "未确认"))
                if not retryable or attempt == 2:
                    raise
                self.sleeper(float(2**attempt))
        raise TextArchiveError("Notion 创建页面失败")

    def replace(self, page_id: str, content: str) -> None:
        self.request("PATCH", f"/pages/{page_id}/markdown", {"type": "replace_content", "replace_content": {"new_str": content}})


def week_number(day: datetime) -> int:
    return (day.day + datetime(day.year, day.month, 1).weekday() - 1) // 7 + 1


def week_title(day: datetime) -> str:
    first = datetime(day.year, day.month, 1)
    start = first + timedelta(days=(week_number(day) - 1) * 7 - first.weekday())
    start = max(first, start)
    end = min(datetime(day.year, day.month, calendar.monthrange(day.year, day.month)[1]), start + timedelta(days=6 - start.weekday()))
    label = f"{start:%m-%d}" if start == end else f"{start:%m-%d}～{end:%m-%d}"
    return f"第 {week_number(day)} 周（{label}）"


def memo_title(memo: dict[str, Any]) -> str:
    return f"{memo['created_at'][:16]} · {memo['memo_uid']}"


def memo_content(memo: dict[str, Any]) -> str:
    # A fence longer than any run in the source preserves literal Markdown,
    # including #tags, @ links, backslashes and blank lines.
    body = memo.get("body_text") or ""
    fence = "`" * max(3, max((len(m.group()) for m in re.finditer(r"`+", body)), default=0) + 1)
    return f"创建时间：{memo['created_at']}（Asia/Shanghai）\n\nflomo ID：{memo['memo_uid']}\n\n{fence}text\n{body}\n{fence}"


def memo_hash(memo: dict[str, Any]) -> str:
    return hashlib.sha256(memo_content(memo).encode("utf-8")).hexdigest()


def memo_blocks(memo: dict[str, Any]) -> list[dict[str, Any]]:
    def paragraph(value: str) -> dict[str, Any]:
        parts = [value[i : i + 1800] for i in range(0, len(value), 1800)]
        return {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": part}} for part in parts
            ]},
        }

    return [
        paragraph(f"创建时间：{memo['created_at']}（Asia/Shanghai）"),
        paragraph(f"flomo ID：{memo['memo_uid']}"),
        paragraph(memo.get("body_text") or ""),
    ]


def state_path(vault: Path) -> Path:
    return vault / "notion-text" / "state.json"


def load_state(vault: Path) -> dict[str, Any]:
    path = state_path(vault)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TextArchiveError("文字归档映射尚未初始化；先执行 notion-text bootstrap") from error
    if state.get("root_page_id") != ROOT_PAGE_ID or not isinstance(state.get("memos"), dict):
        raise TextArchiveError("文字归档映射无效或根页面不匹配")
    return state


def save_state(vault: Path, state: dict[str, Any]) -> None:
    path = state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def bootstrap(vault: Path, metadata: Path, mapping: Path, memos: list[dict[str, Any]]) -> dict[str, Any]:
    if state_path(vault).exists():
        raise TextArchiveError("文字归档映射已存在，不会覆盖")
    meta = json.loads(metadata.read_text(encoding="utf-8"))
    if meta.get("root_page_id") != ROOT_PAGE_ID:
        raise TextArchiveError("回填记录的根页面不匹配")
    local = {m["memo_uid"]: m for m in memos}
    entries: dict[str, Any] = {}
    for line in mapping.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        uid = row["memo_uid"]
        if (
            uid in entries
            or uid not in local
            or row.get("created_at") != local[uid].get("created_at")
            or not row.get("notion_page_id")
        ):
            raise TextArchiveError("回填记录有重复、缺失或无效 memo ID")
        entries[uid] = {"page_id": row["notion_page_id"], "url": row.get("notion_url"), "content_hash": memo_hash(local[uid])}
    if len(entries) != meta.get("memo_count"):
        raise TextArchiveError("回填 memo 数量不一致")
    state = {
        "root_page_id": ROOT_PAGE_ID,
        "years": {str(meta["since"][:4]): meta["year_page_id"]},
        "months": meta["month_page_ids"],
        "weeks": {f"{month}-{index}": page_id for month, ids in meta["week_page_ids"].items() for index, page_id in enumerate(ids, 1)},
        "memos": entries,
    }
    save_state(vault, state)
    return {"status": "bootstrapped", "mapped": len(entries), "path": str(state_path(vault))}


def sync_archive(
    vault: Path,
    memos: list[dict[str, Any]],
    client: TextArchiveClient,
    *,
    since: str = ARCHIVE_SINCE,
    until: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    if not 1 <= workers <= 3:
        raise TextArchiveError("并发写入数应为 1–3")
    client.validate_root()
    if state_path(vault).exists():
        state = load_state(vault)
    else:
        state = {"root_page_id": ROOT_PAGE_ID, "years": {}, "months": {}, "weeks": {}, "memos": {}}
    created = updated = unchanged = 0
    today = until or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    child_cache: dict[str, dict[str, str]] = {}

    def load_children(parent: str) -> None:
        if parent not in child_cache:
            existing: dict[str, str] = {}
            for block in client.children(parent):
                detail = block.get("child_page")
                child_title = detail.get("title") if isinstance(detail, dict) else None
                child_id = block.get("id")
                if not isinstance(child_title, str) or not isinstance(child_id, str):
                    continue
                if child_title in existing and existing[child_title] != child_id:
                    raise TextArchiveError(f"Notion 层级存在同名页面：{child_title}")
                existing[child_title] = child_id
            child_cache[parent] = existing

    def cached_child(
        parent: str, title: str, content: str | None = None,
        fallback_blocks: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str | None]:
        load_children(parent)
        if title in child_cache[parent]:
            return child_cache[parent][title], None
        try:
            page = client.create(parent, title, content, fallback_blocks)
            page_id, url = page["id"], page.get("url")
        except TextArchiveError:
            page_id, url = client.create_or_find(parent, title, content, fallback_blocks)
        child_cache[parent][title] = page_id
        return page_id, url

    def create_new(parent: str, title: str, content: str, blocks: list[dict[str, Any]]) -> tuple[str, str | None]:
        try:
            page = client.create(parent, title, content, blocks)
            return page["id"], page.get("url")
        except TextArchiveError:
            return client.create_or_find(parent, title, content, blocks)

    pending: list[tuple[Future[tuple[str, str | None]], dict[str, Any], str, str, str]] = []
    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None

    def record(memo: dict[str, Any], digest: str, parent: str, title: str, page_id: str, url: str | None) -> None:
        nonlocal created
        child_cache[parent][title] = page_id
        state["memos"][memo["memo_uid"]] = {
            "page_id": page_id,
            "url": url or f"https://app.notion.com/p/{page_id.replace('-', '')}",
            "content_hash": digest,
        }
        save_state(vault, state)
        created += 1
        if progress and created % 100 == 0:
            progress(created, len(state["memos"]))

    def finish_one() -> None:
        future, memo, digest, parent, title = pending.pop(0)
        page_id, url = future.result()
        record(memo, digest, parent, title, page_id, url)

    try:
        for memo in sorted(memos, key=lambda m: (m["created_at"], m["memo_uid"])):
            if memo.get("is_deleted") or not since <= memo["created_at"][:10] <= today:
                continue
            uid = memo["memo_uid"]
            digest = memo_hash(memo)
            entry = state["memos"].get(uid)
            if entry:
                if entry.get("content_hash") == digest:
                    unchanged += 1
                    continue
                while pending:
                    finish_one()
                client.replace(entry["page_id"], memo_content(memo))
                entry["content_hash"] = digest
                save_state(vault, state)
                updated += 1
                continue
            day = datetime.fromisoformat(memo["created_at"])
            year = str(day.year)
            month = f"{day:%Y-%m}"
            week = f"{month}-{week_number(day)}"
            hierarchy = (("years", year, year), ("months", month, f"{day:%m}"), ("weeks", week, week_title(day)))
            parent = ROOT_PAGE_ID
            for kind, key, title in hierarchy:
                page_id = state[kind].get(key)
                if not page_id:
                    page_id, _ = cached_child(parent, title)
                    state[kind][key] = page_id
                    save_state(vault, state)
                parent = page_id
            title = memo_title(memo)
            if executor is None:
                page_id, url = cached_child(parent, title, memo_content(memo), memo_blocks(memo))
                record(memo, digest, parent, title, page_id, url)
            else:
                load_children(parent)
                if title in child_cache[parent]:
                    record(memo, digest, parent, title, child_cache[parent][title], None)
                else:
                    future = executor.submit(create_new, parent, title, memo_content(memo), memo_blocks(memo))
                    pending.append((future, memo, digest, parent, title))
                    if len(pending) >= workers * 2:
                        finish_one()
        while pending:
            finish_one()
    finally:
        if executor:
            executor.shutdown(wait=True)
    return {"status": "complete", "created": created, "updated": updated, "unchanged": unchanged, "mapped": len(state["memos"]), "path": str(state_path(vault))}
