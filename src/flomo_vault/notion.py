"""Read the user's Notion flomo mirror without ever mutating it."""

from __future__ import annotations

import json
import ssl
import subprocess
import time
from datetime import date
from email.message import Message
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import setting


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2026-03-11"
NOTION_DATA_SOURCE_ID = setting("notion_data_source_id")
KEYCHAIN_SERVICE = "flomo-local-vault.notion"
KEYCHAIN_ACCOUNT = NOTION_DATA_SOURCE_ID


class NotionError(RuntimeError):
    """Base error for a read-only Notion operation."""


class NotionAuthError(NotionError):
    """The Notion token is missing or cannot read the configured source."""


class NotionSchemaError(NotionError):
    """The automatic mirror no longer exposes the expected schema."""


def system_trust_urlopen(request: Request, *, timeout: int):
    """Use the macOS CA bundle absent from the framework Python install."""
    bundle = Path("/etc/ssl/cert.pem")
    context = ssl.create_default_context(cafile=str(bundle) if bundle.is_file() else None)
    return urlopen(request, timeout=timeout, context=context)


def read_token() -> str | None:
    if not KEYCHAIN_ACCOUNT:
        return None
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            KEYCHAIN_ACCOUNT,
            "-w",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def store_token(token: str) -> None:
    if not KEYCHAIN_ACCOUNT:
        raise NotionAuthError("Set notion_data_source_id in ~/.config/flomo-vault/config.json first")
    token = token.strip()
    if not token:
        raise NotionAuthError("Notion Token 为空")
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            KEYCHAIN_ACCOUNT,
            "-w",
            token,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise NotionAuthError("无法把 Notion Token 保存到 macOS Keychain")


class NotionReader:
    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., Any] = system_trust_urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._opener = opener
        self._sleep = sleeper

    def _request_json(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            NOTION_API_BASE + path,
            data=data,
            method="POST" if body is not None else "GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            },
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with self._opener(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                last_error = error
                if error.code in {401, 403, 404}:
                    raise NotionAuthError(
                        "Notion 凭证无效，或 Flomo Database 尚未分享给只读 connection"
                    ) from None
                if error.code != 429 and not 500 <= error.code < 600:
                    raise NotionError(f"Notion 读取失败（HTTP {error.code}）") from None
                if attempt < 2:
                    self._sleep(_retry_delay(error.headers, attempt))
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if attempt < 2:
                    self._sleep(2**attempt)
        raise NotionError(f"Notion 读取重试后仍失败：{type(last_error).__name__}")

    def validate_schema(self) -> None:
        payload = self._request_json(f"/data_sources/{NOTION_DATA_SOURCE_ID}")
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            raise NotionSchemaError("Notion Data Source 未返回 properties")
        expected = {"Link": "url", "Created At": "date"}
        mismatches = []
        for name, expected_type in expected.items():
            value = properties.get(name)
            actual = value.get("type") if isinstance(value, dict) else None
            if actual != expected_type:
                mismatches.append(f"{name}={actual or 'missing'}")
        if mismatches:
            raise NotionSchemaError("Notion Schema 已变化：" + ", ".join(mismatches))

    def query_rows(self, since: str, until: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "page_size": 100,
                "filter": {
                    "and": [
                        {"property": "Created At", "date": {"on_or_after": since}},
                        {"property": "Created At", "date": {"on_or_before": until}},
                    ]
                },
                "sorts": [{"property": "Created At", "direction": "ascending"}],
            }
            if cursor:
                body["start_cursor"] = cursor
            payload = self._request_json(f"/data_sources/{NOTION_DATA_SOURCE_ID}/query", body)
            results = payload.get("results")
            if not isinstance(results, list):
                raise NotionError("Notion 查询没有返回 results")
            rows.extend(_normalize_page(page) for page in results if isinstance(page, dict))
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                raise NotionError("Notion 分页缺少 next_cursor")
        return rows


def _retry_delay(headers: Message | None, attempt: int) -> float:
    value = headers.get("Retry-After") if headers is not None else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    return float(2**attempt)


def _property_value(page: dict[str, Any], name: str, kind: str) -> Any:
    properties = page.get("properties")
    value = properties.get(name) if isinstance(properties, dict) else None
    return value.get(kind) if isinstance(value, dict) else None


def _normalize_page(page: dict[str, Any]) -> dict[str, Any]:
    date_value = _property_value(page, "Created At", "date")
    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "created_at": date_value.get("start") if isinstance(date_value, dict) else None,
        "link": _property_value(page, "Link", "url"),
    }


def read_notion_rows(token: str, since: str, until: str) -> list[dict[str, Any]]:
    date.fromisoformat(since)
    date.fromisoformat(until)
    reader = NotionReader(token)
    reader.validate_schema()
    return reader.query_rows(since, until)
