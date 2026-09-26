"""Install and inspect the macOS LaunchAgent for the daily workflow."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


LABEL = "com.flomo.local-vault.daily"
HELPER_BUNDLE_ID = "com.flomo.local-vault.helper"
HELPER_NAME = "Flomo Vault Helper"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "Flomo Vault" / "runtime"


def runtime_python() -> Path:
    return runtime_root() / ".venv" / "bin" / "python"


def runtime_site_packages() -> Path:
    return next((runtime_root() / ".venv" / "lib").glob("python*/site-packages"))


def helper_app_path() -> Path:
    return Path.home() / "Applications" / f"{HELPER_NAME}.app"


def helper_executable() -> Path:
    return helper_app_path() / "Contents" / "MacOS" / "flomo-vault-helper"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_directory() -> Path:
    return Path.home() / "Library" / "Logs" / "Flomo Vault"


def plist_payload() -> dict[str, Any]:
    logs = log_directory()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(helper_executable()), "-m", "flomo_vault.cli", "daily", "--scheduled", "--json"],
        "EnvironmentVariables": {
            "PYTHONHOME": sys.base_prefix,
            "PYTHONPATH": str(runtime_site_packages()),
        },
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": 8, "Minute": 0},
        "StartInterval": 600,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(logs / "daily.stdout.log"),
        "StandardErrorPath": str(logs / "daily.stderr.log"),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def install_schedule() -> Path:
    install_runtime()
    install_helper_app()
    path = launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_directory().mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(plist_payload(), handle, sort_keys=True)
    os.replace(temporary, path)
    _run_launchctl("bootout", _domain(), str(path))
    result = _run_launchctl("bootstrap", _domain(), str(path))
    if result.returncode != 0 and "already bootstrapped" not in result.stderr.lower():
        raise RuntimeError(f"LaunchAgent 安装失败：{result.stderr.strip() or result.stdout.strip()}")
    return path


def install_helper_app() -> Path:
    """Install a stable, dedicated executable identity for macOS privacy grants."""
    destination = helper_app_path()
    executable = helper_executable()
    if executable.is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / f".{HELPER_NAME}.new.app"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "Contents" / "MacOS").mkdir(parents=True)
    source_python = Path(sys._base_executable)
    if not source_python.is_file():
        source_python = runtime_python().resolve()
    staged_executable = stage / "Contents" / "MacOS" / "flomo-vault-helper"
    shutil.copy2(source_python, staged_executable)
    staged_executable.chmod(0o755)
    info = {
        "CFBundleDisplayName": HELPER_NAME,
        "CFBundleExecutable": "flomo-vault-helper",
        "CFBundleIdentifier": HELPER_BUNDLE_ID,
        "CFBundleName": HELPER_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSBackgroundOnly": True,
        "NSAppDataUsageDescription": "读取 flomo 的本地数据以制作只读备份。",
    }
    with (stage / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle, sort_keys=True)
    signed = subprocess.run(
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(stage)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if signed.returncode != 0:
        shutil.rmtree(stage, ignore_errors=True)
        raise RuntimeError(f"后台 Helper 签名失败：{signed.stderr.strip()}")
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(stage, destination)
    return destination


def install_runtime() -> Path:
    """Copy a self-contained runtime outside Documents for background access."""
    source = project_root()
    destination = runtime_root()
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / ".runtime-new"
    backup = parent / ".runtime-old"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    stage.mkdir()
    shutil.copytree(source / ".venv", stage / ".venv", symlinks=True)
    site_packages = next((stage / ".venv" / "lib").glob("python*/site-packages"))
    shutil.rmtree(site_packages / "flomo_vault", ignore_errors=True)
    for path in site_packages.glob("flomo_local_vault-*.dist-info"):
        shutil.rmtree(path, ignore_errors=True)
    for path in site_packages.glob("__editable__*flomo_local_vault*"):
        path.unlink(missing_ok=True)
    shutil.copytree(source / "src" / "flomo_vault", site_packages / "flomo_vault")
    if destination.exists():
        os.replace(destination, backup)
    os.replace(stage, destination)
    shutil.rmtree(backup, ignore_errors=True)
    return destination


def schedule_status() -> dict[str, Any]:
    path = launch_agent_path()
    result = _run_launchctl("print", f"{_domain()}/{LABEL}")
    return {
        "label": LABEL,
        "path": str(path),
        "installed": path.is_file(),
        "loaded": result.returncode == 0,
        "hour": 8,
        "minute": 0,
    }


def uninstall_schedule() -> Path:
    path = launch_agent_path()
    _run_launchctl("bootout", _domain(), str(path))
    if path.exists():
        path.unlink()
    return path
