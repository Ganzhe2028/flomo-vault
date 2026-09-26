"""Small orchestration helpers shared by manual and scheduled runs."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterator


class AlreadyRunningError(RuntimeError):
    pass


MAX_FOREGROUND_DEFERRALS = 6


@contextlib.contextmanager
def exclusive_run(vault_root: Path) -> Iterator[None]:
    vault_root.mkdir(parents=True, exist_ok=True)
    lock_path = vault_root / ".sync.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunningError("已有一个 flomo 同步任务正在运行") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def scheduled_state_path(vault_root: Path) -> Path:
    return vault_root / ".daily-state.json"


def scheduled_run_due(vault_root: Path, now: datetime, hour: int = 8) -> bool:
    if now.hour < hour:
        return False
    path = scheduled_state_path(vault_root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    today = now.date().isoformat()
    completed = state.get("last_completed_date") or state.get("last_success_date")
    return completed != today and state.get("gave_up_date") != today


def mark_scheduled_success(vault_root: Path, now: datetime) -> None:
    mark_scheduled_complete(vault_root, now, success=True)


def mark_scheduled_complete(vault_root: Path, now: datetime, *, success: bool) -> None:
    state: dict[str, object] = {
        "last_completed_date": now.date().isoformat(),
        "last_exit_was_success": success,
        "updated_at": now.isoformat(),
    }
    if success:
        state["last_success_date"] = now.date().isoformat()
    _write_scheduled_state(
        vault_root,
        state,
    )


def record_foreground_deferral(vault_root: Path, now: datetime) -> tuple[int, bool]:
    path = scheduled_state_path(vault_root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    today = now.date().isoformat()
    attempts = int(state.get("foreground_deferrals", 0)) + 1 if state.get("deferral_date") == today else 1
    gave_up = attempts >= MAX_FOREGROUND_DEFERRALS
    updated = {
        **state,
        "deferral_date": today,
        "foreground_deferrals": attempts,
        "updated_at": now.isoformat(),
    }
    if gave_up:
        updated["gave_up_date"] = today
    _write_scheduled_state(vault_root, updated)
    return attempts, gave_up


def _write_scheduled_state(vault_root: Path, state: dict[str, object]) -> None:
    path = scheduled_state_path(vault_root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def notify_failure(title: str, message: str) -> None:
    script = "display notification " + _apple_string(message) + " with title " + _apple_string(title)
    subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _apple_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
