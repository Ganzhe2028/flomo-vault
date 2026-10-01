"""Refresh expiring flomo media URLs and download files incrementally."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import struct
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


API_ROOT = "https://flomoapp.com/api/v1"
API_KEY = "flomo_web"
SIGNING_SECRET = "dbbc3dd73364b4084c3a69346e0ce2b2"
DEFAULT_CACHE_ROOT = Path.home() / "Library/Containers/com.flomoapp.m/Data/Library/Application Support/flomo/Cache/Cache_Data"


def app_version() -> str:
    plist = Path("/Applications/flomo.app/Contents/Info.plist")
    try:
        with plist.open("rb") as handle:
            info = plistlib.load(handle)
        return str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "5.26.72")
    except (OSError, ValueError):
        return "5.26.72"


def signed_query(
    values: list[tuple[str, str]],
    version: str,
    timestamp: int | None = None,
) -> str:
    params = list(values)
    params.extend(
        [
            ("timestamp", str(timestamp if timestamp is not None else int(time.time()))),
            ("api_key", API_KEY),
            ("app_version", version),
            ("platform", "mac"),
            ("webp", "1"),
        ]
    )
    params.sort(key=lambda pair: pair[0])
    query = urllib.parse.urlencode(params)
    sign = hashlib.md5((query + SIGNING_SECRET).encode()).hexdigest()
    return query + "&sign=" + sign


class FlomoApiClient:
    def __init__(self, token: str, version: str | None = None, timeout: int = 30) -> None:
        self._token = token
        self.version = version or app_version()
        self.timeout = timeout

    def _request_json(self, url: str) -> Any:
        def request_with(auth_value: str) -> Any:
            config = _curl_config(url, {"Authorization": auth_value, "Accept": "application/json", "User-Agent": f"flomo/{self.version} macOS"})
            last_code = 0
            for attempt in range(3):
                result = subprocess.run(
                    ["/usr/bin/curl", "--silent", "--show-error", "--fail", "--location", "--max-time", str(self.timeout), "--config", "-"],
                    input=config,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                last_code = result.returncode
                if result.returncode == 0:
                    return json.loads(result.stdout)
                if attempt < 2:
                    time.sleep(2**attempt)
            raise RuntimeError(f"flomo 附件接口请求失败（curl {last_code}）")

        payload = request_with(self._token)
        if isinstance(payload, dict) and payload.get("code") == -10 and not self._token.lower().startswith("bearer "):
            payload = request_with("Bearer " + self._token)
        return payload

    def refresh_files(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        values = [("ids[]", str(value)) for value in ids]
        url = API_ROOT + "/file/?" + signed_query(values, self.version)
        payload = self._request_json(url)
        if not isinstance(payload, dict) or payload.get("code") not in (0, "0", None):
            raise RuntimeError("flomo 附件地址刷新失败")
        data = payload.get("data", payload)
        if isinstance(data, dict):
            for key in ("files", "items", "list"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise RuntimeError("flomo 附件接口返回了无法识别的数据格式")
        return {
            str(item.get("id") or item.get("uid")): item
            for item in data
            if isinstance(item, dict) and (item.get("id") is not None or item.get("uid") is not None)
        }


def _safe_extension(attachment: dict[str, Any], remote: dict[str, Any]) -> str:
    for value in (remote.get("path"), remote.get("name"), attachment.get("path"), attachment.get("name"), remote.get("url")):
        if not isinstance(value, str):
            continue
        suffix = Path(urllib.parse.urlsplit(value).path).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            return suffix
    return ".bin"


def media_relative_path(attachment: dict[str, Any], remote: dict[str, Any]) -> Path:
    created = str(attachment.get("memo_created_at") or "unknown")
    year = created[:4] if re.fullmatch(r"\d{4}", created[:4]) else "unknown"
    month = created[:7] if re.fullmatch(r"\d{4}-\d{2}", created[:7]) else "unknown"
    identifier = re.sub(r"[^A-Za-z0-9._-]", "_", str(attachment["attachment_id"]))
    return Path("media") / year / month / f"{identifier}{_safe_extension(attachment, remote)}"


def _download(url: str, destination: Path, timeout: int = 120) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part", dir=destination.parent)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as output:
            result = subprocess.run(
                ["/usr/bin/curl", "--silent", "--show-error", "--fail", "--location", "--max-time", str(timeout), "--config", "-"],
                input=_curl_config(url, {"User-Agent": "flomo-vault/0.1"}),
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("附件下载失败")
            output.flush()
            os.fsync(output.fileno())
        with open(temp_name, "rb") as downloaded:
            while chunk := downloaded.read(1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
        os.replace(temp_name, destination)
        return digest.hexdigest(), total
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise


def _extract_image(data: bytes, suffix: str) -> bytes | None:
    suffix = suffix.lower()
    if suffix == ".png":
        signature = b"\x89PNG\r\n\x1a\n"
        start = data.find(signature)
        if start < 0:
            return None
        cursor = start + len(signature)
        while cursor + 12 <= len(data):
            length = struct.unpack(">I", data[cursor : cursor + 4])[0]
            chunk_type = data[cursor + 4 : cursor + 8]
            cursor += 12 + length
            if cursor > len(data):
                return None
            if chunk_type == b"IEND":
                return data[start:cursor]
        return None
    if suffix in {".jpg", ".jpeg"}:
        start = data.find(b"\xff\xd8\xff")
        end = data.rfind(b"\xff\xd9")
        return data[start : end + 2] if start >= 0 and end > start else None
    if suffix == ".webp":
        start = data.find(b"RIFF")
        if start < 0 or data[start + 8 : start + 12] != b"WEBP":
            return None
        end = start + 8 + struct.unpack("<I", data[start + 4 : start + 8])[0]
        return data[start:end] if end <= len(data) else None
    if suffix == ".gif":
        starts = [value for value in (data.find(b"GIF87a"), data.find(b"GIF89a")) if value >= 0]
        if not starts:
            return None
        start = min(starts)
        end = data.rfind(b"\x3b")
        return data[start : end + 1] if end > start else None
    return None


def _cache_matches(cache_root: Path, name: str):
    """Find a filename in cache entries without depending on an external command."""
    needle = name.encode("utf-8")
    for directory, _, filenames in os.walk(cache_root):
        for filename in filenames:
            path = Path(directory) / filename
            try:
                with path.open("rb") as handle:
                    overlap = b""
                    while chunk := handle.read(1024 * 1024):
                        data = overlap + chunk
                        if needle in data:
                            yield path
                            break
                        overlap = data[-(len(needle) - 1):] if len(needle) > 1 else b""
            except OSError:
                continue


def _recover_image_from_cache(
    attachment: dict[str, Any],
    destination: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> tuple[str, int] | None:
    name = attachment.get("name")
    if not isinstance(name, str) or not name or not cache_root.is_dir():
        return None
    expected = attachment.get("size")
    for path in _cache_matches(cache_root, name):
        try:
            payload = _extract_image(path.read_bytes(), Path(name).suffix)
        except OSError:
            continue
        if not payload:
            continue
        if isinstance(expected, int) and expected > 0 and len(payload) != expected:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".cache-part", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, destination)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temp_name).unlink(missing_ok=True)
            raise
        return hashlib.sha256(payload).hexdigest(), len(payload)
    return None


def _curl_config(url: str, headers: dict[str, str]) -> bytes:
    """Put credentials and signed URLs on stdin, never in process arguments."""
    def quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")

    lines = [f'url = "{quote(url)}"']
    lines.extend(f'header = "{quote(name + ": " + value)}"' for name, value in headers.items())
    return ("\n".join(lines) + "\n").encode()


def sync_media(
    attachments: list[dict[str, Any]],
    vault_root: Path,
    token: str | None,
    mode: str = "new",
    workers: int = 6,
    limit: int | None = None,
) -> dict[str, int]:
    stats = {"total": len(attachments), "downloaded": 0, "recovered": 0, "existing": 0, "failed": 0, "deferred": 0}
    if mode == "none":
        for attachment in attachments:
            attachment["download_status"] = "not_requested"
        stats["deferred"] = len(attachments)
        return stats
    if not token:
        for attachment in attachments:
            attachment["download_status"] = "missing_token"
        stats["failed"] = len(attachments)
        return stats

    client = FlomoApiClient(token)
    candidates: list[dict[str, Any]] = []
    for attachment in attachments:
        candidate_path = media_relative_path(attachment, attachment)
        full_path = vault_root / candidate_path
        if mode == "new" and full_path.is_file():
            attachment["media_relpath"] = candidate_path.as_posix()
            attachment["download_status"] = "existing"
            attachment["bytes"] = full_path.stat().st_size
            stats["existing"] += 1
        else:
            candidates.append(attachment)
    if limit is not None:
        skipped = candidates[limit:]
        candidates = candidates[:limit]
        for attachment in skipped:
            attachment["download_status"] = "deferred_by_limit"
        stats["deferred"] += len(skipped)

    refreshed: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(candidates), 100):
        batch = candidates[offset : offset + 100]
        try:
            refreshed.update(client.refresh_files([row["attachment_id"] for row in batch]))
        except Exception:
            for attachment in batch:
                attachment["download_status"] = "refresh_failed"

    jobs: dict[Any, tuple[dict[str, Any], Path]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for attachment in candidates:
            if attachment.get("download_status") == "refresh_failed":
                relative = media_relative_path(attachment, attachment)
                attachment["media_relpath"] = relative.as_posix()
                recovered = _recover_image_from_cache(attachment, vault_root / relative)
                if recovered:
                    sha256, size = recovered
                    attachment["download_status"] = "recovered_from_cache"
                    attachment["sha256"] = sha256
                    attachment["bytes"] = size
                    stats["downloaded"] += 1
                    stats["recovered"] += 1
                else:
                    stats["failed"] += 1
                continue
            remote = refreshed.get(attachment["attachment_id"], {})
            url = remote.get("url") or attachment.get("remote_url")
            if not isinstance(url, str) or not url:
                attachment["download_status"] = "missing_url"
                stats["failed"] += 1
                continue
            relative = media_relative_path(attachment, remote)
            attachment["media_relpath"] = relative.as_posix()
            jobs[pool.submit(_download, url, vault_root / relative)] = (attachment, relative)
        for future in as_completed(jobs):
            attachment, _ = jobs[future]
            try:
                sha256, size = future.result()
                attachment["download_status"] = "downloaded"
                attachment["sha256"] = sha256
                attachment["bytes"] = size
                stats["downloaded"] += 1
            except Exception:
                recovered = _recover_image_from_cache(attachment, vault_root / str(attachment["media_relpath"]))
                if recovered:
                    sha256, size = recovered
                    attachment["download_status"] = "recovered_from_cache"
                    attachment["sha256"] = sha256
                    attachment["bytes"] = size
                    stats["downloaded"] += 1
                    stats["recovered"] += 1
                else:
                    attachment["download_status"] = "download_failed"
                    stats["failed"] += 1
    return stats
