"""Command-line interface for the flomo local vault."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .app_control import (
    DEFAULT_DATA_ROOT,
    FlomoInUseError,
    consistent_snapshot,
    is_flomo_frontmost,
    is_flomo_running,
)
from .database import parse_snapshot, select_data
from .config import setting
from .exporter import publish_snapshot
from .local_storage import read_access_token
from .media import sync_media
from .notion import NOTION_DATA_SOURCE_ID, NotionError, NotionReader, read_token, store_token
from .notion_text import (
    ARCHIVE_SINCE,
    ROOT_PAGE_ID,
    TextArchiveClient,
    TextArchiveError,
    bootstrap as bootstrap_text_archive,
    read_text_token,
    store_text_token,
    sync_archive,
)
from .reconciliation import build_reconciliation, load_local_memos, normalize_input, publish_reconciliation
from .schedule import install_schedule, schedule_status, uninstall_schedule
from .workflow import (
    AlreadyRunningError,
    exclusive_run,
    mark_scheduled_complete,
    notify_failure,
    record_foreground_deferral,
    scheduled_run_due,
)


DEFAULT_VAULT = Path.home() / "Documents/Flomo Vault"
DAILY_SINCE = setting("daily_since", "1970-01-01")
DAILY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError("日期格式应为 YYYY-MM-DD") from error


def _media_types(value: str) -> set[str]:
    allowed = {"image", "live_photo", "recorded", "audio"}
    selected = {item.strip() for item in value.split(",") if item.strip()}
    unknown = selected - allowed
    if not selected or unknown:
        raise argparse.ArgumentTypeError(f"附件类型应从 {', '.join(sorted(allowed))} 中选择")
    return selected


def _progress(message: str, *, json_output: bool) -> None:
    print(message, file=sys.stderr if json_output else sys.stdout, flush=True)


def _perform_sync(
    *,
    vault: Path,
    data_root: Path,
    source_snapshot: Path | None,
    since: str | None,
    until: str | None,
    media_types: set[str] | None,
    media_mode: str,
    workers: int,
    retention: int,
    sync_wait: float,
    reopen: bool,
    media_limit: int | None,
    ensure_fresh: bool,
    stabilize_timeout: float,
    abort_if_frontmost: bool,
    json_output: bool,
    archive_memos: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    _progress("正在制作一致快照…", json_output=json_output)
    with consistent_snapshot(
        source_snapshot=source_snapshot,
        data_root=data_root,
        sync_wait=sync_wait,
        reopen=reopen,
        ensure_fresh=ensure_fresh,
        stabilize_timeout=stabilize_timeout,
        abort_if_frontmost=abort_if_frontmost,
    ) as snapshot:
        _progress("正在解析笔记、标签、历史和附件索引…", json_output=json_output)
        data = parse_snapshot(snapshot)
        if archive_memos is not None:
            archive_memos.extend(data.memos)
        selection: dict[str, object] = {}
        if since or until or media_types:
            data = select_data(data, since, until, media_types)
            selection = {
                "since": since,
                "until": until,
                "attachment_types": sorted(media_types) if media_types else None,
            }
        token = read_access_token(snapshot) if media_mode != "none" else None
        _progress(f"已读取 {len(data.memos)} 条笔记、{len(data.attachments)} 条附件记录。", json_output=json_output)
        media_stats = sync_media(
            data.attachments,
            vault,
            token,
            mode=media_mode,
            workers=workers,
            limit=media_limit,
        )
    return publish_snapshot(
        vault,
        data,
        media_stats,
        retention=retention,
        source_label="provided-snapshot" if source_snapshot else "local-app-snapshot",
        selection=selection,
    )


def _sync(args: argparse.Namespace) -> int:
    vault = _path(args.vault)
    if args.abort_if_frontmost and is_flomo_frontmost():
        print("flomo 正在前台使用；本次文字快照已取消。", file=sys.stderr)
        return 2
    manifest = _perform_sync(
        vault=vault,
        data_root=_path(args.data_root),
        source_snapshot=_path(args.source_snapshot) if args.source_snapshot else None,
        since=args.since,
        until=args.until,
        media_types=args.media_types,
        media_mode=args.media,
        workers=args.workers,
        retention=args.retention,
        sync_wait=args.sync_wait,
        reopen=not args.no_reopen,
        media_limit=args.media_limit,
        ensure_fresh=False,
        stabilize_timeout=60,
        abort_if_frontmost=args.abort_if_frontmost,
        json_output=args.json,
    )
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    else:
        changes = manifest["changes"]
        media = manifest["media"]
        print(
            f"同步完成：{manifest['counts']['memos']} 条笔记；"
            f"新增 {changes['added']}，更新 {changes['updated']}；"
            f"附件新下载 {media['downloaded']}，失败 {media['failed']}。"
        )
        print(f"当前结果：{vault / 'current'}")
    return 0


def _daily(args: argparse.Namespace) -> int:
    vault = _path(args.vault)
    now = datetime.now(DAILY_TIMEZONE)
    if args.scheduled and not args.force and not scheduled_run_due(vault, now):
        result = {"status": "skipped", "reason": "already_completed_or_before_08:00"}
        print(json.dumps(result, ensure_ascii=False) if args.json else "今天的自动同步无需重复运行。")
        return 0
    if args.scheduled and is_flomo_frontmost():
        return _defer_for_foreground(args, vault, now)
    try:
        with exclusive_run(vault):
            archive_memos: list[dict[str, object]] = []
            manifest = _perform_sync(
                vault=vault,
                data_root=_path(args.data_root),
                source_snapshot=_path(args.source_snapshot) if args.source_snapshot else None,
                since=DAILY_SINCE,
                until=now.date().isoformat(),
                media_types={"image"},
                media_mode="new",
                workers=args.workers,
                retention=args.retention,
                sync_wait=0,
                reopen=True,
                media_limit=args.media_limit,
                ensure_fresh=not bool(args.source_snapshot),
                stabilize_timeout=args.stabilize_timeout,
                abort_if_frontmost=args.scheduled,
                json_output=args.json,
                archive_memos=archive_memos,
            )
            local_manifest, memos = load_local_memos(vault)
            notion_token = read_token()
            notion_rows: list[dict[str, object]] = []
            notion_error: str | None = None
            reader: NotionReader | None = None
            if notion_token is None:
                notion_error = "notion_token_missing" if NOTION_DATA_SOURCE_ID else "notion_not_configured"
            else:
                try:
                    reader = NotionReader(notion_token)
                    reader.validate_schema()
                    notion_rows = reader.query_rows(DAILY_SINCE, now.date().isoformat())
                except NotionError as error:
                    notion_error = str(error)
            report, mapping = build_reconciliation(
                local_manifest, memos, notion_rows, "notion-api", error=notion_error
            )
            if (
                notion_error is None
                and reader is not None
                and args.notion_lag_wait > 0
                and (report["counts"]["pending_notion"] or report["counts"]["notion_only"])
            ):
                _progress("Notion 尚有差异，15 秒后复查一次…", json_output=args.json)
                time.sleep(args.notion_lag_wait)
                try:
                    notion_rows = reader.query_rows(DAILY_SINCE, now.date().isoformat())
                    report, mapping = build_reconciliation(local_manifest, memos, notion_rows, "notion-api")
                except NotionError as error:
                    report, mapping = build_reconciliation(
                        local_manifest, memos, [], "notion-api", error=str(error)
                    )
            publish_reconciliation(vault, report, mapping)
            text_archive: dict[str, object] = {"status": "not_configured"}
            text_token = read_text_token()
            if text_token:
                try:
                    text_archive = sync_archive(vault, archive_memos, TextArchiveClient(text_token))
                except TextArchiveError as error:
                    text_archive = {"status": "failed", "error": str(error)}
                except Exception as error:
                    text_archive = {"status": "failed", "error": f"unexpected_{type(error).__name__}"}
            degraded = (
                manifest["status"] != "complete"
                or (bool(NOTION_DATA_SOURCE_ID) and report["status"] in {"invalid", "unavailable"})
                or text_archive["status"] == "failed"
            )
            exit_code = 2 if degraded else 0
            result = {
                "status": "degraded" if degraded else "complete",
                "local": manifest,
                "reconciliation": report,
                "text_archive": text_archive,
                "vault": str(vault / "current"),
                "reconciliation_path": str(vault / "reconciliation" / "current"),
            }
            if args.scheduled:
                mark_scheduled_complete(vault, now, success=exit_code == 0)
            if args.scheduled and not args.force and exit_code == 2:
                message = _degraded_message(manifest, report)
                if text_archive["status"] == "failed":
                    message += "；文字归档写入失败。"
                notify_failure("flomo 自动同步需要留意", message)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(_daily_summary(manifest, report, text_archive))
            return exit_code
    except AlreadyRunningError as error:
        return _daily_failure(args, 2, str(error))
    except FlomoInUseError:
        return _defer_for_foreground(args, vault, now)
    except Exception as error:
        return _daily_failure(args, 1, str(error))


def _defer_for_foreground(args: argparse.Namespace, vault: Path, now: datetime) -> int:
    attempts, gave_up = record_foreground_deferral(vault, now)
    if gave_up:
        message = "flomo 连续约一小时处于前台，今天未自动同步；旧版资料库保持不变。"
        notify_failure("flomo 自动同步已跳过", message)
    result = {
        "status": "skipped_flomo_in_use" if gave_up else "deferred_flomo_in_use",
        "foreground_deferrals": attempts,
        "retry_in_seconds": None if gave_up else 600,
        "previous_vault_preserved": True,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("flomo 正在前台使用；不会退出它。" + ("今天已停止重试。" if gave_up else "约 10 分钟后重试。"))
    return 0


def _daily_failure(args: argparse.Namespace, code: int, message: str) -> int:
    if args.scheduled:
        notify_failure("flomo 自动同步失败", message)
    if args.json:
        print(json.dumps({"status": "failed", "exit_code": code, "error": message}, ensure_ascii=False))
    else:
        print(f"同步失败：{message}", file=sys.stderr)
        print("上一次完整同步结果未受影响。", file=sys.stderr)
    return code


def _degraded_message(manifest: dict[str, object], report: dict[str, object]) -> str:
    media = manifest["media"]
    counts = report["counts"]
    return (
        f"图片失败 {media['failed']}；Notion 状态 {report['status']}；"
        f"待追平 {counts['pending_notion']}，非法 {counts['invalid_rows']}。"
    )


def _daily_summary(manifest: dict[str, object], report: dict[str, object], text_archive: dict[str, object]) -> str:
    counts = report["counts"]
    media = manifest["media"]
    summary = (
        f"同步完成：{manifest['counts']['memos']} 条 memo，图片失败 {media['failed']}；"
        f"Notion 已对应 {counts['matched']}，待追平 {counts['pending_notion']}，"
        f"仅 Notion {counts['notion_only']}。"
    )
    if text_archive["status"] == "complete":
        summary += f" 文字归档新增 {text_archive['created']}，更新 {text_archive['updated']}。"
    elif text_archive["status"] == "not_configured":
        summary += " 文字归档尚未配置写入权限。"
    else:
        summary += " 文字归档写入失败，详情见 JSON 结果。"
    return summary


def _reconcile(args: argparse.Namespace) -> int:
    vault = _path(args.vault)
    try:
        payload = json.load(sys.stdin)
        rows = normalize_input(payload)
        with exclusive_run(vault):
            manifest, memos = load_local_memos(vault)
            report, mapping = build_reconciliation(manifest, memos, rows, args.source)
            publish_reconciliation(vault, report, mapping)
    except AlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"校对输入无效：{error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else _reconcile_summary(report))
    return 2 if report["status"] == "invalid" else 0


def _reconcile_summary(report: dict[str, object]) -> str:
    counts = report["counts"]
    return (
        f"校对完成：已对应 {counts['matched']}，待追平 {counts['pending_notion']}，"
        f"仅 Notion {counts['notion_only']}，非法 {counts['invalid_rows']}，重复 {counts['duplicate_memo_ids']}。"
    )


def _notion_setup(args: argparse.Namespace) -> int:
    del args
    if not NOTION_DATA_SOURCE_ID:
        print("先在 ~/.config/flomo-vault/config.json 配置 notion_data_source_id。", file=sys.stderr)
        return 2
    token = getpass.getpass("粘贴 Notion 只读 Connection Token（输入不会显示）：").strip()
    try:
        reader = NotionReader(token)
        reader.validate_schema()
        reader.query_rows(DAILY_SINCE, DAILY_SINCE)
        store_token(token)
    except NotionError as error:
        print(f"Notion 配置失败：{error}", file=sys.stderr)
        return 2
    print("Notion 只读连接已验证，Token 已保存到 macOS Keychain。")
    return 0


def _notion_text(args: argparse.Namespace) -> int:
    vault = _path(args.vault)
    try:
        if args.action == "setup":
            token = getpass.getpass("粘贴文字归档专用 Notion Connection Token（输入不会显示）：").strip()
            if not token:
                raise TextArchiveError("Token 为空")
            TextArchiveClient(token).validate_root()
            store_text_token(token)
            result: dict[str, object] = {"status": "configured", "root_page_id": ROOT_PAGE_ID}
        elif args.action == "bootstrap":
            if not args.metadata or not args.mapping:
                raise TextArchiveError("bootstrap 需要 --metadata 和 --mapping")
            _, memos = load_local_memos(vault)
            result = bootstrap_text_archive(vault, _path(args.metadata), _path(args.mapping), memos)
        elif args.action == "sync":
            token = read_text_token()
            if not token:
                raise TextArchiveError("未配置文字归档 Token；先执行 notion-text setup")
            with exclusive_run(vault):
                source_vault = _path(args.source_vault) if args.source_vault else vault
                _, memos = load_local_memos(source_vault)
                result = sync_archive(
                    vault, memos, TextArchiveClient(token), since=args.since or ARCHIVE_SINCE, until=args.until,
                    progress=lambda created, mapped: _progress(
                        f"文字归档已新增 {created} 条；当前映射 {mapped} 条。", json_output=args.json
                    ),
                    workers=args.workers,
                )
        else:
            state = (vault / "notion-text" / "state.json")
            result = {"status": "configured" if read_text_token() else "not_configured", "mapping_exists": state.is_file(), "mapping_path": str(state)}
    except (TextArchiveError, OSError, ValueError, AlreadyRunningError) as error:
        result = {"status": "failed", "error": str(error)}
        print(json.dumps(result, ensure_ascii=False) if args.json else f"文字归档失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else f"文字归档：{result}")
    return 0


def _schedule(args: argparse.Namespace) -> int:
    if args.action == "install":
        path = install_schedule()
        print(f"每天 08:00 的自动同步已安装：{path}")
        return 0
    if args.action == "uninstall":
        path = uninstall_schedule()
        print(f"自动同步已移除：{path}")
        return 0
    status = schedule_status()
    print(json.dumps(status, ensure_ascii=False, sort_keys=True) if args.json else _schedule_summary(status))
    return 0 if status["installed"] and status["loaded"] else 2


def _schedule_summary(status: dict[str, object]) -> str:
    return (
        f"自动同步：{'已安装' if status['installed'] else '未安装'}，"
        f"{'已加载' if status['loaded'] else '未加载'}；每天 08:00。"
    )


def _status(args: argparse.Namespace) -> int:
    vault = _path(args.vault)
    manifest_path = vault / "current" / "manifest.json"
    if not manifest_path.is_file():
        print(f"还没有可用同步结果：{vault}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reconciliation_path = vault / "reconciliation" / "current" / "reconcile.json"
    reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8")) if reconciliation_path.is_file() else None
    if args.json:
        print(json.dumps({"local": manifest, "reconciliation": reconciliation}, ensure_ascii=False, sort_keys=True))
    else:
        counts = manifest["counts"]
        media = manifest["media"]
        print(f"状态：{manifest['status']}　同步时间：{manifest['created_at']}")
        print(f"笔记：{counts['memos']}（有效 {counts['active_memos']}，已删除 {counts['deleted_memos']}）")
        print(f"附件：{counts['attachments']}（本次失败 {media['failed']}，未处理 {media['deferred']}）")
        if reconciliation:
            print(_reconcile_summary(reconciliation))
        print(f"位置：{vault / 'current'}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    data_root = _path(args.data_root)
    checks = {
        "flomo_app": Path("/Applications/flomo.app").is_dir(),
        "local_data": data_root.is_dir(),
        "indexeddb": (data_root / "IndexedDB").is_dir(),
        "local_storage": (data_root / "Local Storage" / "leveldb").is_dir(),
        "flomo_running": is_flomo_running(),
        "notion_keychain": read_token() is not None,
    }
    for name, okay in checks.items():
        marker = "正常" if okay else ("未运行" if name == "flomo_running" else "缺失")
        print(f"{name}: {marker}")
    required = all(checks[name] for name in ("flomo_app", "local_data", "indexeddb", "local_storage"))
    print("结论：可以同步。" if required else "结论：环境不完整，请看上面的缺失项。")
    return 0 if required else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flomo-vault", description="从 flomo Mac 本地数据库重建并校对只读 Notion 镜像")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="执行一次安全的通用本地同步")
    _add_sync_arguments(sync)
    sync.set_defaults(func=_sync)

    daily = subparsers.add_parser("daily", help="同步配置日期以来的文字和图片，并校对 Notion")
    daily.add_argument("--vault", default=str(DEFAULT_VAULT))
    daily.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), help=argparse.SUPPRESS)
    daily.add_argument("--source-snapshot", help=argparse.SUPPRESS)
    daily.add_argument("--workers", type=int, default=6)
    daily.add_argument("--retention", type=int, default=7)
    daily.add_argument("--stabilize-timeout", type=float, default=60.0, help=argparse.SUPPRESS)
    daily.add_argument("--notion-lag-wait", type=float, default=15.0, help=argparse.SUPPRESS)
    daily.add_argument("--media-limit", type=int, help=argparse.SUPPRESS)
    daily.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    daily.add_argument("--force", action="store_true", help="忽略当天已成功标记，强制运行")
    daily.add_argument("--json", action="store_true")
    daily.set_defaults(func=_daily)

    notion = subparsers.add_parser("notion", help="配置 Notion 只读连接")
    notion_sub = notion.add_subparsers(dest="notion_action", required=True)
    notion_setup = notion_sub.add_parser("setup", help="验证并保存只读 Token 到 Keychain")
    notion_setup.set_defaults(func=_notion_setup)

    notion_text = subparsers.add_parser("notion-text", help="管理独立的纯文字 Notion 归档")
    notion_text.add_argument("action", choices=("setup", "bootstrap", "sync", "status"))
    notion_text.add_argument("--vault", default=str(DEFAULT_VAULT))
    notion_text.add_argument("--metadata", help="首次回填的 notion-text-export.json")
    notion_text.add_argument("--mapping", help="首次回填的 memo_notion_text_map.jsonl")
    notion_text.add_argument("--source-vault", help="从另一份本地文字快照读取 memo，映射仍写入 --vault")
    notion_text.add_argument("--since", type=_date)
    notion_text.add_argument("--until", type=_date)
    notion_text.add_argument("--workers", type=int, choices=(1, 2, 3), default=1, help="批量回填时的并发写入数，最多 3")
    notion_text.add_argument("--json", action="store_true")
    notion_text.set_defaults(func=_notion_text)

    reconcile = subparsers.add_parser("reconcile", help="从 stdin 接收 Notion JSON 并校对当前 Vault")
    reconcile.add_argument("--vault", default=str(DEFAULT_VAULT))
    reconcile.add_argument("--stdin", action="store_true", required=True)
    reconcile.add_argument("--source", default="codex-notion")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(func=_reconcile)

    schedule = subparsers.add_parser("schedule", help="管理每天 08:00 的 macOS LaunchAgent")
    schedule.add_argument("action", choices=("install", "status", "uninstall"))
    schedule.add_argument("--json", action="store_true")
    schedule.set_defaults(func=_schedule)

    status = subparsers.add_parser("status", help="查看最近一次本地同步和 Notion 校对状态")
    status.add_argument("--vault", default=str(DEFAULT_VAULT))
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_status)

    doctor = subparsers.add_parser("doctor", help="检查本机同步条件")
    doctor.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), help=argparse.SUPPRESS)
    doctor.set_defaults(func=_doctor)
    return parser


def _add_sync_arguments(sync: argparse.ArgumentParser) -> None:
    sync.add_argument("--vault", default=str(DEFAULT_VAULT), help="资料库位置")
    sync.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), help=argparse.SUPPRESS)
    sync.add_argument("--source-snapshot", help="从已有快照同步（用于恢复或测试）")
    sync.add_argument("--media", choices=("none", "new", "all"), default="new")
    sync.add_argument("--since", type=_date)
    sync.add_argument("--until", type=_date)
    sync.add_argument("--media-types", type=_media_types)
    sync.add_argument("--workers", type=int, default=6)
    sync.add_argument("--retention", type=int, default=7)
    sync.add_argument("--sync-wait", type=float, default=3.0, help=argparse.SUPPRESS)
    sync.add_argument("--no-reopen", action="store_true", help=argparse.SUPPRESS)
    sync.add_argument("--media-limit", type=int, help=argparse.SUPPRESS)
    sync.add_argument("--json", action="store_true")
    sync.add_argument("--abort-if-frontmost", action="store_true", help="flomo 在前台时不打断使用")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("已取消；上一次完整同步结果未受影响。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
