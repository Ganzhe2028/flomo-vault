"""Safely make a consistent, minimal snapshot of the flomo app data."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path


APP_BUNDLE_ID = "com.flomoapp.m"
DEFAULT_DATA_ROOT = Path.home() / "Library/Containers/com.flomoapp.m/Data/Library/Application Support/flomo"


class FlomoInUseError(RuntimeError):
    """Raised when an automatic run would interrupt foreground flomo use."""


def is_flomo_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "/Applications/flomo.app/Contents/MacOS/flomo"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def frontmost_bundle_id() -> str | None:
    script = (
        'ObjC.import("AppKit"); '
        "const app=$.NSWorkspace.sharedWorkspace.frontmostApplication; "
        'app ? ObjC.unwrap(app.bundleIdentifier) : ""'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def is_flomo_frontmost() -> bool:
    return frontmost_bundle_id() == APP_BUNDLE_ID


def quit_flomo(timeout: float = 20.0) -> None:
    # NSRunningApplication terminates the app through AppKit.  Unlike
    # ``tell application ... to quit`` this does not send an Apple Event, so a
    # background run never asks for Automation permission.
    script = (
        'ObjC.import("AppKit"); '
        f'const apps=$.NSRunningApplication.runningApplicationsWithBundleIdentifier("{APP_BUNDLE_ID}"); '
        "for (let i=0; i<apps.count; i++) { apps.objectAtIndex(i).terminate(); }"
    )
    subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.monotonic() + timeout
    while is_flomo_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if is_flomo_running():
        raise RuntimeError("flomo 未能在 20 秒内正常退出；没有强制结束，也没有开始复制")


def open_flomo(*, background: bool = False) -> None:
    previous_bundle_id = frontmost_bundle_id() if background else None
    arguments = ["/usr/bin/open"]
    if background:
        # -g avoids activation and -j asks LaunchServices to launch hidden.
        arguments.extend(["-g", "-j"])
    arguments.extend(["-b", APP_BUNDLE_ID])
    subprocess.run(
        arguments,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if background:
        _keep_flomo_in_background(previous_bundle_id)


def _keep_flomo_in_background(previous_bundle_id: str | None, timeout: float = 8.0) -> None:
    """Hide flomo after launch and restore the app the user was working in."""
    deadline = time.monotonic() + timeout
    while not is_flomo_running() and time.monotonic() < deadline:
        time.sleep(0.1)
    previous = previous_bundle_id or ""
    script = (
        'ObjC.import("AppKit"); '
        f'const flomo=$.NSRunningApplication.runningApplicationsWithBundleIdentifier("{APP_BUNDLE_ID}"); '
        "for (let i=0; i<flomo.count; i++) { flomo.objectAtIndex(i).hide(); } "
        f'const previous=$.NSRunningApplication.runningApplicationsWithBundleIdentifier("{previous}"); '
        "if (previous.count > 0) { previous.objectAtIndex(0).activateWithOptions(1 << 1); }"
    )
    subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _data_fingerprint(data_root: Path) -> tuple[int, int, int]:
    """Return a cheap fingerprint that changes while Chromium data is being written."""
    files = 0
    total_size = 0
    latest_mtime_ns = 0
    for relative in ("IndexedDB", "Local Storage"):
        root = data_root / relative
        if not root.is_dir():
            continue
        for directory, _, names in os.walk(root):
            for name in names:
                try:
                    stat = (Path(directory) / name).stat()
                except FileNotFoundError:
                    continue
                files += 1
                total_size += stat.st_size
                latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
    return files, total_size, latest_mtime_ns


def wait_for_flomo(timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_flomo_running():
            return
        time.sleep(0.25)
    raise RuntimeError("flomo 未能在 10 秒内启动")


def wait_for_data_stable(
    data_root: Path,
    timeout: float = 60.0,
    poll_interval: float = 2.0,
    stable_samples: int = 3,
) -> None:
    """Wait until flomo's local data stops changing for several samples."""
    deadline = time.monotonic() + timeout
    previous: tuple[int, int, int] | None = None
    stable = 0
    while time.monotonic() < deadline:
        current = _data_fingerprint(data_root)
        if current == previous and current[0] > 0:
            stable += 1
            if stable >= stable_samples:
                return
        else:
            stable = 0
        previous = current
        time.sleep(poll_interval)
    raise RuntimeError("等待 flomo 本地数据稳定超时；没有开始复制")


def copy_snapshot(source: Path, destination: Path) -> None:
    required = ("IndexedDB", "Local Storage")
    for name in required:
        source_path = source / name
        if not source_path.is_dir():
            raise FileNotFoundError(f"flomo 数据目录缺少 {name}：{source_path}")
        shutil.copytree(source_path, destination / name)


@contextlib.contextmanager
def consistent_snapshot(
    source_snapshot: Path | None = None,
    data_root: Path = DEFAULT_DATA_ROOT,
    sync_wait: float = 3.0,
    reopen: bool = True,
    ensure_fresh: bool = False,
    stabilize_timeout: float = 60.0,
    abort_if_frontmost: bool = False,
) -> Iterator[Path]:
    """Yield a stable snapshot while restoring the app's original running state."""
    if source_snapshot is not None:
        yield source_snapshot
        return

    if not data_root.is_dir():
        raise FileNotFoundError(f"没有找到 flomo 本地数据：{data_root}")
    was_running = is_flomo_running()
    started_for_sync = False
    stopped_for_copy = False
    user_took_over = False
    temp_root = Path(tempfile.mkdtemp(prefix="flomo-vault-snapshot-"))
    try:
        if abort_if_frontmost and was_running and is_flomo_frontmost():
            raise FlomoInUseError("flomo 正在前台使用；本次自动同步已延后")
        if ensure_fresh and not was_running:
            open_flomo(background=abort_if_frontmost)
            wait_for_flomo()
            started_for_sync = True
        if was_running or started_for_sync:
            if ensure_fresh:
                wait_for_data_stable(data_root, timeout=stabilize_timeout)
            elif sync_wait > 0:
                time.sleep(sync_wait)
            if abort_if_frontmost and is_flomo_frontmost():
                user_took_over = True
                raise FlomoInUseError("同步准备期间 flomo 回到前台；本次自动同步已延后")
            quit_flomo()
            stopped_for_copy = True
        try:
            copy_snapshot(data_root, temp_root)
        finally:
            if was_running and reopen and stopped_for_copy:
                open_flomo(background=abort_if_frontmost)
        yield temp_root
    finally:
        if started_for_sync and is_flomo_running() and not user_took_over:
            try:
                quit_flomo()
            except RuntimeError:
                pass
        shutil.rmtree(temp_root, ignore_errors=True)
