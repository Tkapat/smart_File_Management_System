"""Command-line interface for SmartFS."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sqlite3
import sys
from contextlib import suppress
from pathlib import Path

from smartfs import __version__, db
from smartfs.analysis import AnalysisError, AnalysisService, analysis_label
from smartfs.config import (
    AppPaths,
    ConfigError,
    Settings,
    configure_logging,
    load_settings,
    redact_secrets,
    save_settings,
)
from smartfs.credentials import (
    CredentialError,
    credential_status,
    delete_api_key,
    environment_api_key,
    set_api_key,
)
from smartfs.doctor import run_doctor
from smartfs.embeddings import (
    EmbeddingService,
    embedding_label,
    fastembed_available,
)
from smartfs.models import Category, FileRecord, FileStatus, TagSource
from smartfs.related import RelatedError, related_files
from smartfs.scanner import ScanError, ScanStats, normalize_root, scan_directories
from smartfs.search import SearchError, SearchHit, SearchMode, search_files
from smartfs.tags import (
    TagError,
    add_tag,
    merge_tags,
    normalize_tag,
    normalize_tags,
    remove_tag,
    rename_tag,
)
from smartfs.xattr_store import (
    XattrError,
    XattrOperationError,
    XattrSupport,
    XattrUnsupported,
    list_smartfs_xattrs,
    probe_xattr_support,
)

LOGGER = logging.getLogger("smartfs.cli")


def _mode_label(settings: Settings) -> str:
    if settings.local_only or not settings.consent:
        return "Local-only (heuristic analysis, no cloud calls)"
    return "Cloud AI enabled (Gemini with local fallback)"


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging to stderr and the rotating log file",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the SmartFS argument parser."""
    parser = argparse.ArgumentParser(
        prog="smartfs",
        description="Semantic file indexing with Linux xattrs and AI-ready metadata.",
    )
    parser.add_argument("--version", action="version", version=f"smartfs {__version__}")
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create configuration and database")
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="do not ask questions (automatic for non-TTY use)",
    )
    init_parser.set_defaults(handler=_cmd_init)

    watch_parser = subparsers.add_parser("watch", help="manage watched directories")
    watch_sub = watch_parser.add_subparsers(dest="watch_action", required=True)
    watch_add = watch_sub.add_parser("add", help="opt a directory into watching")
    watch_add.add_argument("directory")
    watch_add.set_defaults(handler=_cmd_watch_add)
    watch_remove = watch_sub.add_parser("remove", help="remove a watched directory")
    watch_remove.add_argument("directory")
    watch_remove.set_defaults(handler=_cmd_watch_remove)
    watch_list = watch_sub.add_parser("list", help="list watched directories")
    watch_list.set_defaults(handler=_cmd_watch_list)

    scan_parser = subparsers.add_parser("scan", help="index files recursively")
    scan_parser.add_argument(
        "paths",
        nargs="*",
        help="directories to scan (default: configured watched directories)",
    )
    scan_parser.add_argument(
        "--once",
        action="store_true",
        help="run one scan and exit (default)",
    )
    scan_parser.add_argument(
        "--force", action="store_true", help="re-hash and re-extract unchanged files"
    )
    scan_parser.set_defaults(handler=_cmd_scan)

    tags_parser = subparsers.add_parser(
        "tags", help="show database tags and raw xattrs side by side"
    )
    tags_parser.add_argument("file")
    tags_parser.set_defaults(handler=_cmd_tags)

    tag_parser = subparsers.add_parser("tag", help="add, remove, rename, or merge tags")
    tag_sub = tag_parser.add_subparsers(dest="tag_action", required=True)
    tag_add = tag_sub.add_parser("add", help="add a user tag")
    tag_add.add_argument("file")
    tag_add.add_argument("tag")
    tag_add.set_defaults(handler=_cmd_tag_add)
    tag_remove = tag_sub.add_parser("remove", help="remove a tag")
    tag_remove.add_argument("file")
    tag_remove.add_argument("tag")
    tag_remove.add_argument(
        "--source",
        choices=("any", "user", "ai"),
        default="any",
        help="only remove tags from one provenance source",
    )
    tag_remove.set_defaults(handler=_cmd_tag_remove)
    tag_rename = tag_sub.add_parser("rename", help="rename a tag across the index")
    tag_rename.add_argument("old")
    tag_rename.add_argument("new")
    tag_rename.set_defaults(handler=_cmd_tag_rename)
    tag_merge = tag_sub.add_parser("merge", help="merge one tag into another")
    tag_merge.add_argument("source")
    tag_merge.add_argument("target")
    tag_merge.set_defaults(handler=_cmd_tag_merge)

    duplicates_parser = subparsers.add_parser(
        "duplicates", help="list files that share identical content"
    )
    duplicates_parser.add_argument("--limit", type=int, default=100)
    duplicates_parser.set_defaults(handler=_cmd_duplicates)

    related_parser = subparsers.add_parser(
        "related", help="find documents related to an indexed file"
    )
    related_parser.add_argument("file")
    related_parser.add_argument("--limit", type=int, default=10)
    related_parser.set_defaults(handler=_cmd_related)

    collection_parser = subparsers.add_parser("collection", help="manage named saved searches")
    collection_sub = collection_parser.add_subparsers(dest="collection_action", required=True)
    collection_list = collection_sub.add_parser("list", help="list saved searches")
    collection_list.set_defaults(handler=_cmd_collection_list)
    collection_show = collection_sub.add_parser("show", help="show one saved search")
    collection_show.add_argument("name")
    collection_show.set_defaults(handler=_cmd_collection_show)
    collection_save = collection_sub.add_parser("save", help="create or replace a saved search")
    collection_save.add_argument("name")
    collection_save.add_argument("--tag", action="append", default=[])
    collection_save.add_argument("--category")
    collection_save.add_argument(
        "--mode",
        choices=("auto", "text", "semantic", "hybrid"),
        default="auto",
    )
    collection_save.add_argument("query", nargs="*")
    collection_save.set_defaults(handler=_cmd_collection_save)
    collection_delete = collection_sub.add_parser("delete", help="delete a saved search")
    collection_delete.add_argument("name")
    collection_delete.set_defaults(handler=_cmd_collection_delete)

    find_parser = subparsers.add_parser(
        "find", help="find files with filters and hybrid text/semantic search"
    )
    find_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="require a semantic tag (repeatable)",
    )
    find_parser.add_argument("--category", help="require an exact category")
    find_parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in SearchMode),
        default=SearchMode.AUTO.value,
        help="auto uses hybrid search when embeddings are available (default: auto)",
    )
    find_parser.add_argument("--explain", action="store_true", help="show why each file matched")
    find_parser.add_argument("--limit", type=int, default=100)
    find_parser.add_argument("query", nargs="*", help="full-text or semantic query")
    find_parser.set_defaults(handler=_cmd_find)

    gui_parser = subparsers.add_parser("gui", help="launch the PySide6 desktop interface")
    gui_parser.add_argument(
        "--scan",
        action="store_true",
        help="start an incremental scan immediately after the window opens",
    )
    gui_parser.set_defaults(handler=_cmd_gui)

    daemon_parser = subparsers.add_parser(
        "daemon", help="run filesystem watching and indexing workers in the foreground"
    )
    daemon_parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="number of indexing workers (default: config worker_count)",
    )
    daemon_parser.add_argument(
        "--reconcile-interval",
        type=int,
        default=None,
        metavar="SECONDS",
        help="periodic full reconciliation interval (default: config value)",
    )
    daemon_parser.add_argument(
        "--no-startup-reconcile",
        action="store_true",
        help="skip the immediate full reconciliation at startup",
    )
    daemon_parser.set_defaults(handler=_cmd_daemon)

    ai_parser = subparsers.add_parser("ai", help="configure consent, Gemini key, and embeddings")
    ai_sub = ai_parser.add_subparsers(dest="ai_action", required=True)
    ai_status = ai_sub.add_parser("status", help="show AI, credential, and embedding status")
    ai_status.set_defaults(handler=_cmd_ai_status)
    ai_setup = ai_sub.add_parser("setup", help="interactively configure consent and optional key")
    ai_setup.add_argument(
        "--yes",
        action="store_true",
        help="enable cloud analysis without prompts when a key is already configured",
    )
    ai_setup.add_argument(
        "--no-embeddings",
        action="store_true",
        help="leave local embeddings disabled",
    )
    ai_setup.set_defaults(handler=_cmd_ai_setup)
    ai_enable = ai_sub.add_parser("enable", help="enable consented Gemini analysis")
    ai_enable.add_argument(
        "--embeddings",
        action="store_true",
        help="also enable local fastembed embeddings",
    )
    ai_enable.set_defaults(handler=_cmd_ai_enable)
    ai_disable = ai_sub.add_parser("disable", help="disable cloud analysis and return to local")
    ai_disable.set_defaults(handler=_cmd_ai_disable)
    ai_set_key = ai_sub.add_parser("set-key", help="store a Gemini API key in the system keyring")
    ai_set_key.add_argument(
        "--stdin",
        action="store_true",
        help="read the key from one stdin line instead of a hidden prompt",
    )
    ai_set_key.set_defaults(handler=_cmd_ai_set_key)
    ai_forget_key = ai_sub.add_parser("forget-key", help="remove the key from the system keyring")
    ai_forget_key.set_defaults(handler=_cmd_ai_forget_key)
    ai_embeddings = ai_sub.add_parser(
        "embeddings", help="enable or disable local embeddings without cloud access"
    )
    embeddings_sub = ai_embeddings.add_subparsers(dest="embeddings_action", required=True)
    embeddings_enable = embeddings_sub.add_parser("enable", help="enable local semantic embeddings")
    embeddings_enable.set_defaults(handler=_cmd_ai_embeddings_enable)
    embeddings_disable = embeddings_sub.add_parser("disable", help="disable local embeddings")
    embeddings_disable.set_defaults(handler=_cmd_ai_embeddings_disable)

    doctor_parser = subparsers.add_parser(
        "doctor", help="diagnose index integrity and repair derived metadata"
    )
    doctor_parser.add_argument("--repair", action="store_true", help="repair safely derived data")
    doctor_parser.add_argument(
        "--vacuum", action="store_true", help="compact the SQLite database with VACUUM"
    )
    doctor_parser.set_defaults(handler=_cmd_doctor)

    backup_parser = subparsers.add_parser("backup", help="create, list, or prune backups")
    backup_sub = backup_parser.add_subparsers(dest="backup_action", required=True)
    backup_create = backup_sub.add_parser("create", help="create a consistent backup")
    backup_create.add_argument("--keep", type=int, default=10)
    backup_create.set_defaults(handler=_cmd_backup_create)
    backup_list = backup_sub.add_parser("list", help="list database backups")
    backup_list.set_defaults(handler=_cmd_backup_list)
    backup_prune = backup_sub.add_parser("prune", help="delete old backups")
    backup_prune.add_argument("--keep", type=int, default=10)
    backup_prune.set_defaults(handler=_cmd_backup_prune)

    status_parser = subparsers.add_parser("status", help="show index, queue, and xattr status")
    status_parser.set_defaults(handler=_cmd_status)

    return parser


def _require_database(paths: AppPaths) -> None:
    if not paths.db_path.exists():
        raise ConfigError(f"Database not found at {paths.db_path}. Run 'smartfs init' first.")


def _open_database(paths: AppPaths, settings: Settings) -> sqlite3.Connection:
    _require_database(paths)
    db.init_database(
        paths.db_path,
        backup_dir=paths.backup_dir,
        backup_keep=5,
    )
    return db.connect(paths.db_path)


def _analysis_services(
    settings: Settings, paths: AppPaths
) -> tuple[AnalysisService | None, EmbeddingService | None]:
    analysis = AnalysisService(settings) if settings.analysis_enabled else None
    embeddings = (
        EmbeddingService(settings, paths.cache_dir / "fastembed")
        if settings.embeddings_enabled
        else None
    )
    return analysis, embeddings


def _resolve_path(raw: str) -> str:
    return normalize_root(raw)


def _indexed_file(connection: sqlite3.Connection, raw_path: str) -> tuple[str, FileRecord]:
    path = _resolve_path(raw_path)
    record = db.get_file_by_path(connection, path)
    if record is None:
        raise ConfigError(
            f"File is not indexed: {path}. Run 'smartfs scan {Path(path).parent}' first."
        )
    return path, record


def _print_xattr_support(path: str, support: XattrSupport) -> None:
    if support.supported:
        print(f"  xattrs: supported ({path})")
    else:
        detail = support.reason or "unsupported"
        print(f"  xattrs: DB-only ({detail}; {path})")


def _cmd_init(args: argparse.Namespace, paths: AppPaths) -> int:
    paths.ensure()
    config_exists = paths.config_file.exists()
    settings = load_settings(paths)
    if not config_exists:
        save_settings(settings, paths)
    db.init_database(paths.db_path, backup_dir=paths.backup_dir, backup_keep=5)
    print(f"Config: {paths.config_file}")
    print(f"Database: {paths.db_path}")
    print(f"Logs: {paths.log_file}")
    print(f"Mode: {_mode_label(settings)}")
    print(
        "Analysis defaults to local heuristics and makes no network calls. "
        "Enable Gemini explicitly with 'smartfs ai setup'."
    )
    print("Next: smartfs watch add ~/Documents && smartfs scan")
    print("Then run: smartfs gui, smartfs daemon, or smartfs ai setup")
    return 0


def _cmd_watch_add(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    directory = _resolve_path(args.directory)
    if not os.path.isdir(directory):
        raise ConfigError(f"Not a directory: {directory}")
    if directory not in settings.watched_dirs:
        settings.watched_dirs.append(directory)
        save_settings(settings, paths)
        print(f"Added watched directory: {directory}")
    else:
        print(f"Already watched: {directory}")
    support = probe_xattr_support(directory)
    _print_xattr_support(directory, support)
    return 0


def _cmd_watch_remove(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    directory = _resolve_path(args.directory)
    remaining = [item for item in settings.watched_dirs if item != directory]
    if len(remaining) == len(settings.watched_dirs):
        raise ConfigError(f"Directory is not watched: {directory}")
    settings.watched_dirs = remaining
    save_settings(settings, paths)
    print(f"Removed watched directory: {directory}")
    print("Existing index rows are retained; use 'smartfs scan' or doctor in later phases.")
    return 0


def _cmd_watch_list(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    print(f"Mode: {_mode_label(settings)}")
    if not settings.watched_dirs:
        print("No watched directories. Add one with: smartfs watch add <dir>")
        return 0
    for directory in settings.watched_dirs:
        exists = os.path.isdir(directory)
        print(f"- {directory}{'' if exists else ' (missing)'}")
        if exists:
            _print_xattr_support(directory, probe_xattr_support(directory))
    return 0


def _print_scan_stats(stats: ScanStats) -> None:
    print(f"Scanned: {stats.root}")
    print(
        "  "
        f"seen={stats.files_seen} added={stats.added} updated={stats.updated} "
        f"unchanged={stats.unchanged} unsupported={stats.unsupported} "
        f"missing={stats.missing} skipped={stats.skipped} errors={stats.errors} "
        f"xattr_synced={stats.xattr_synced} xattr_unsupported={stats.xattr_unsupported} "
        f"xattr_errors={stats.xattr_errors}"
    )
    print(
        "  "
        f"analyzed={stats.analyzed} analysis_cached={stats.analysis_cached} "
        f"analysis_failures={stats.analysis_failures} embedded={stats.embedded} "
        f"embedding_cached={stats.embedding_cached} "
        f"embedding_failures={stats.embedding_failures}"
    )


def _cmd_scan(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    roots = args.paths or list(settings.watched_dirs)
    if not roots:
        raise ConfigError("No scan roots. Use 'smartfs scan <dir>' or add a watched directory.")
    if not settings.xattr_enabled:
        print("Note: xattr mirroring is disabled in config; SQLite remains authoritative.")
    connection = _open_database(paths, settings)
    analysis_service, embedding_service = _analysis_services(settings, paths)
    if embedding_service is not None and not fastembed_available():
        print(
            "Note: embeddings are enabled but fastembed is not installed; "
            "run 'uv sync --extra ai' to generate vectors."
        )
    if analysis_service is not None and analysis_service.cloud_requested:
        credential = credential_status()
        if not credential.available:
            print(
                "Note: cloud consent is enabled but no API key is configured; using local fallback."
            )
    exit_code = 0
    aggregated = ScanStats(root="")
    try:
        for root in roots:
            try:
                result = scan_directories(
                    [root],
                    connection,
                    settings,
                    force=args.force,
                    analysis_service=analysis_service,
                    embedding_service=embedding_service,
                )[0]
            except ScanError as exc:
                print(f"error: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            _print_scan_stats(result)
            aggregated.files_seen += result.files_seen
            aggregated.added += result.added
            aggregated.updated += result.updated
            aggregated.unchanged += result.unchanged
            aggregated.unsupported += result.unsupported
            aggregated.skipped += result.skipped
            aggregated.errors += result.errors
            aggregated.missing += result.missing
            aggregated.xattr_synced += result.xattr_synced
            aggregated.xattr_unsupported += result.xattr_unsupported
            aggregated.xattr_errors += result.xattr_errors
            aggregated.analyzed += result.analyzed
            aggregated.analysis_cached += result.analysis_cached
            aggregated.analysis_failures += result.analysis_failures
            aggregated.embedded += result.embedded
            aggregated.embedding_cached += result.embedding_cached
            aggregated.embedding_failures += result.embedding_failures
            if result.errors:
                exit_code = 1
    finally:
        connection.close()
    if len(roots) > 1:
        print(
            "Total: "
            f"seen={aggregated.files_seen} added={aggregated.added} "
            f"updated={aggregated.updated} unchanged={aggregated.unchanged} "
            f"unsupported={aggregated.unsupported} missing={aggregated.missing} "
            f"errors={aggregated.errors} analyzed={aggregated.analyzed} "
            f"embedded={aggregated.embedded}"
        )
    if aggregated.xattr_unsupported:
        print(
            "xattr: unsupported on some files; tags remain available in SQLite. "
            "Use a filesystem mounted with user_xattr (ext4/xfs/btrfs) for the mirror."
        )
    print(f"Mode: {_mode_label(settings)}")
    return exit_code


def _confirm(prompt: str, *, default: bool) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().casefold()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _cmd_ai_status(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    credential = credential_status()
    print(f"Analysis: {analysis_label(settings)}")
    print(f"Consent: {'enabled' if settings.consent else 'disabled'}")
    print(f"Local-only: {'yes' if settings.local_only else 'no'}")
    print(f"Gemini model: {settings.gemini_model}")
    if credential.available:
        print(f"API key: configured ({credential.source})")
    else:
        detail = credential.detail or "not configured"
        print(f"API key: missing ({detail})")
    print(f"Embeddings: {embedding_label(settings)}")
    print(f"Rate limit: {settings.rate_limit_per_minute} Gemini requests/minute")
    if not paths.db_path.exists():
        print("Index: not initialized. Run 'smartfs init'.")
        return 0
    db.init_database(paths.db_path, backup_dir=paths.backup_dir, backup_keep=5)
    connection = db.connect(paths.db_path, read_only=True)
    try:
        analysis_counts = db.analysis_stats(connection)
        if analysis_counts:
            summary = ", ".join(
                f"{name}={count}" for name, count in sorted(analysis_counts.items())
            )
            print(f"Analyzed files: {summary}")
        embedding_counts = db.embedding_stats(connection)
        if embedding_counts.get("total", 0):
            summary = ", ".join(
                f"{name}={count}"
                for name, count in sorted(embedding_counts.items())
                if name != "total"
            )
            print(f"Embeddings stored: total={embedding_counts['total']} ({summary})")
        else:
            print("Embeddings stored: 0")
    finally:
        connection.close()
    return 0


def _cmd_ai_setup(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    credential = credential_status()
    interactive = sys.stdin.isatty() and not args.yes
    if not interactive and not args.yes:
        raise ConfigError(
            "Non-interactive setup requires --yes and an existing key in the "
            "environment or keyring."
        )

    enable_cloud = args.yes
    if interactive:
        enable_cloud = _confirm(
            "Send document excerpts to Google Gemini for analysis?", default=False
        )

    if enable_cloud:
        if not credential.available:
            if not interactive:
                raise ConfigError(
                    "No Gemini API key is configured. Set GEMINI_API_KEY or run "
                    "'smartfs ai set-key' first."
                )
            key = getpass.getpass("Gemini API key (leave empty to cancel): ").strip()
            if not key:
                raise ConfigError("No API key entered; cloud analysis was not enabled.")
            set_api_key(key)
            credential = credential_status()
            if not credential.available:
                raise ConfigError("The key was stored but could not be read back.")
        settings.analysis_enabled = True
        settings.consent = True
        settings.local_only = False
    else:
        settings.consent = False
        settings.local_only = True

    enable_embeddings = not args.no_embeddings
    if interactive:
        enable_embeddings = enable_embeddings and _confirm(
            "Enable local embeddings (downloads the model on first use)?",
            default=True,
        )
    settings.embeddings_enabled = enable_embeddings
    save_settings(settings, paths)
    print(f"Analysis: {analysis_label(settings)}")
    print(f"API key: {credential.source if credential.available else 'not configured'}")
    print(f"Embeddings: {embedding_label(settings)}")
    print("Settings saved.")
    return 0


def _cmd_ai_enable(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    credential = credential_status()
    if not credential.available:
        raise ConfigError(
            "No Gemini API key is configured. Set GEMINI_API_KEY or run 'smartfs ai set-key' first."
        )
    settings.analysis_enabled = True
    settings.consent = True
    settings.local_only = False
    if args.embeddings:
        settings.embeddings_enabled = True
    save_settings(settings, paths)
    print(f"Cloud analysis enabled with {settings.gemini_model}.")
    print(f"API key source: {credential.source}")
    print(f"Embeddings: {embedding_label(settings)}")
    print("Restart a running daemon to apply the change.")
    return 0


def _cmd_ai_disable(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    settings.consent = False
    settings.local_only = True
    save_settings(settings, paths)
    print("Cloud analysis disabled; local heuristic analysis remains active.")
    print("The stored API key was left untouched.")
    print("Restart a running daemon to apply the change.")
    return 0


def _cmd_ai_set_key(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.stdin:
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass("Gemini API key: ").strip()
    if not key:
        raise ConfigError("No API key provided.")
    set_api_key(key)
    print("API key stored in the system keyring.")
    environment, variable = environment_api_key()
    if environment is not None:
        print(f"Note: {variable} takes precedence over the keyring key.")
    return 0


def _cmd_ai_embeddings_enable(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    settings.embeddings_enabled = True
    save_settings(settings, paths)
    print(f"Embeddings enabled: {embedding_label(settings)}")
    print("The model is downloaded on first use. Restart a running daemon to apply.")
    return 0


def _cmd_ai_embeddings_disable(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    settings.embeddings_enabled = False
    save_settings(settings, paths)
    print("Embeddings disabled. Existing vectors remain stored but will not be queried.")
    print("Restart a running daemon to apply.")
    return 0


def _cmd_ai_forget_key(args: argparse.Namespace, paths: AppPaths) -> int:
    removed = delete_api_key()
    if removed:
        print("Removed the Gemini API key from the system keyring.")
    else:
        print("No Gemini API key was stored in the system keyring.")
    environment, variable = environment_api_key()
    if environment is not None:
        print(f"Note: {variable} is still configured and takes precedence.")
    return 0


def _cmd_tag_rename(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    connection = _open_database(paths, settings)
    try:
        results = rename_tag(
            connection,
            args.old,
            args.new,
            suppression_ttl=settings.self_write_suppression_seconds,
        )
    finally:
        connection.close()
    if not results:
        print("Tag name is unchanged.")
        return 0
    print(f"Renamed tag across {len(results)} file(s).")
    unsupported = sum(1 for result in results if not result.supported)
    if unsupported:
        print(f"xattrs: {unsupported} file(s) use DB-only metadata.")
    return 0


def _cmd_tag_merge(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    connection = _open_database(paths, settings)
    try:
        results = merge_tags(
            connection,
            args.source,
            args.target,
            suppression_ttl=settings.self_write_suppression_seconds,
        )
    finally:
        connection.close()
    if not results:
        print("Source and target tags are the same.")
        return 0
    print(f"Merged tag into {args.target!r} across {len(results)} file(s).")
    unsupported = sum(1 for result in results if not result.supported)
    if unsupported:
        print(f"xattrs: {unsupported} file(s) use DB-only metadata.")
    return 0


def _cmd_duplicates(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.limit < 1:
        raise ConfigError("--limit must be positive.")
    _require_database(paths)
    connection = db.connect(paths.db_path, read_only=True)
    try:
        groups = db.duplicate_groups(connection, limit=args.limit)
    finally:
        connection.close()
    if not groups:
        print("No duplicate files found.")
        return 0
    total_files = sum(len(group.files) for group in groups)
    print(f"{len(groups)} duplicate group(s), {total_files} file(s)")
    for group in groups:
        print(f"- {len(group.files)} copies, {group.size} bytes, sha256={group.sha256}")
        for record in group.files:
            print(f"    {record.path}")
    return 0


def _cmd_related(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.limit < 1:
        raise ConfigError("--limit must be positive.")
    settings = load_settings(paths)
    _require_database(paths)
    connection = db.connect(paths.db_path, read_only=True)
    try:
        _path, record = _indexed_file(connection, args.file)
        if record.id is None:
            raise ConfigError("Indexed file has no id")
        hits = related_files(connection, settings, int(record.id), limit=args.limit)
    finally:
        connection.close()
    if not hits:
        print("No related files found.")
        return 0
    print(f"Related to: {record.path}")
    for hit in hits:
        print(f"- {hit.record.path}")
        print(f"    why: {hit.reason}; score={hit.score:.3f}")
    return 0


def _cmd_collection_list(args: argparse.Namespace, paths: AppPaths) -> int:
    _require_database(paths)
    connection = db.connect(paths.db_path, read_only=True)
    try:
        collections = db.list_collections(connection)
    finally:
        connection.close()
    if not collections:
        print("No saved searches.")
        return 0
    for collection in collections:
        tags = ", ".join(collection.tags) or "none"
        print(
            f"- {collection.name}: query={collection.query!r} tags=[{tags}] "
            f"category={collection.category or '(any)'} mode={collection.mode}"
        )
    return 0


def _cmd_collection_show(args: argparse.Namespace, paths: AppPaths) -> int:
    _require_database(paths)
    connection = db.connect(paths.db_path, read_only=True)
    try:
        collection = db.get_collection(connection, args.name)
    finally:
        connection.close()
    if collection is None:
        raise ConfigError(f"Collection not found: {args.name}")
    print(f"Name: {collection.name}")
    print(f"Query: {collection.query or '(none)'}")
    print(f"Tags: {', '.join(collection.tags) if collection.tags else '(none)'}")
    print(f"Category: {collection.category or '(any)'}")
    print(f"Mode: {collection.mode}")
    return 0


def _cmd_collection_save(args: argparse.Namespace, paths: AppPaths) -> int:
    _require_database(paths)
    try:
        tags = normalize_tags(args.tag)
    except TagError as exc:
        raise ConfigError(str(exc)) from exc
    category = args.category.strip().lower() if args.category else None
    connection = db.connect(paths.db_path)
    try:
        with db.transaction(connection):
            collection = db.save_collection(
                connection,
                args.name,
                query=" ".join(args.query),
                tags=tags,
                category=category,
                mode=args.mode,
            )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    finally:
        connection.close()
    print(f"Saved collection: {collection.name}")
    return 0


def _cmd_collection_delete(args: argparse.Namespace, paths: AppPaths) -> int:
    _require_database(paths)
    connection = db.connect(paths.db_path)
    try:
        with db.transaction(connection):
            removed = db.delete_collection(connection, args.name)
    finally:
        connection.close()
    if not removed:
        raise ConfigError(f"Collection not found: {args.name}")
    print(f"Deleted collection: {args.name}")
    return 0


def _cmd_doctor(args: argparse.Namespace, paths: AppPaths) -> int:
    _require_database(paths)
    settings = load_settings(paths)
    report = run_doctor(
        paths,
        settings,
        repair=args.repair,
        vacuum=args.vacuum,
    )
    print(f"Database: {paths.db_path}")
    print(f"Schema: {report.counts.get('schema_version', 'unknown')}")
    print(
        "Files: "
        f"done={report.counts.get('done', 0)} "
        f"missing={report.counts.get('missing', 0)} "
        f"failed={report.counts.get('failed', 0)} "
        f"unsupported={report.counts.get('unsupported', 0)}"
    )
    print(
        "Full-text: "
        f"{report.counts.get('fts_documents', 0)} document(s), "
        f"missing={report.counts.get('fts_missing', 0)}, "
        f"orphans={report.counts.get('fts_orphans', 0)}"
    )
    print(f"Embeddings: {report.counts.get('embeddings', 0)}")
    if report.backup is not None:
        print(f"Pre-repair backup: {report.backup}")
    if report.issues:
        print("Findings:")
        for issue in report.issues:
            state = " (repaired)" if issue.repaired else ""
            print(f"- [{issue.severity}] {issue.code}: {issue.message}{state}")
    else:
        print("Findings: none")
    if report.warnings:
        print("Warnings:")
        for warning in report.warnings:
            print(f"- {warning}")
    if report.repaired:
        print("Repairs:")
        for action in report.repaired:
            print(f"- {action}")
    if report.healthy:
        print("Status: healthy")
        return 0
    print(f"Status: {report.errors} unresolved error(s)")
    return 1


def _cmd_backup_create(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.keep < 1:
        raise ConfigError("--keep must be at least 1.")
    _require_database(paths)
    target = db.backup_database(
        paths.db_path,
        paths.backup_dir,
        keep=args.keep,
        prefix="manual",
    )
    print(f"Backup created: {target}")
    return 0


def _cmd_backup_list(args: argparse.Namespace, paths: AppPaths) -> int:
    backups = db.list_backups(paths.backup_dir)
    if not backups:
        print("No backups found.")
        return 0
    for backup in backups:
        try:
            size = backup.stat().st_size
        except OSError:
            size = 0
        print(f"- {backup} ({size} bytes)")
    return 0


def _cmd_backup_prune(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.keep < 1:
        raise ConfigError("--keep must be at least 1.")
    removed = db.prune_backups(paths.backup_dir, keep=args.keep)
    if not removed:
        print("No backups removed.")
        return 0
    print(f"Removed {len(removed)} old backup(s):")
    for path in removed:
        print(f"- {path}")
    return 0


def _cmd_gui(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    _require_database(paths)
    db.init_database(paths.db_path, backup_dir=paths.backup_dir, backup_keep=5)
    try:
        from smartfs.gui import run_gui
    except ImportError as exc:
        raise ConfigError(
            "The GUI requires PySide6. Install it with 'uv sync --extra gui'."
        ) from exc
    return run_gui(paths, settings, scan_on_start=args.scan)


def _cmd_daemon(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    if not settings.watched_dirs:
        raise ConfigError("No watched directories. Use 'smartfs watch add <dir>' first.")
    if args.workers is not None and not 1 <= args.workers <= 32:
        raise ConfigError("--workers must be between 1 and 32.")
    if args.reconcile_interval is not None and args.reconcile_interval < 10:
        raise ConfigError("--reconcile-interval must be at least 10 seconds.")
    try:
        from smartfs.daemon import (
            SmartFSDaemon,
            install_signal_handlers,
            restore_signal_handlers,
        )
        from smartfs.watcher import WatcherError
    except ImportError as exc:
        raise ConfigError("The daemon requires watchdog. Reinstall with 'uv sync'.") from exc

    daemon = SmartFSDaemon(
        paths,
        settings,
        worker_count=args.workers,
        reconcile_interval=args.reconcile_interval,
        startup_reconcile=not args.no_startup_reconcile,
    )
    previous = install_signal_handlers(daemon.stop_event)
    try:
        try:
            daemon.start()
        except WatcherError as exc:
            raise ConfigError(str(exc)) from exc
        print(f"SmartFS {__version__} daemon (foreground)")
        print(f"PID: {os.getpid()}")
        print(f"Workers: {daemon.worker_count}")
        print(f"Reconcile interval: {daemon.reconcile_interval}s")
        print("Watched directories:")
        for directory in settings.watched_dirs:
            state = "ok" if os.path.isdir(directory) else "missing"
            print(f"- {directory} ({state})")
        print("Queue: persistent (SQLite)")
        print("Press Ctrl+C to stop.")
        daemon.wait()
    finally:
        restore_signal_handlers(previous)
        daemon.stop()
    print("Stopped.")
    return 0


def _cmd_tags(args: argparse.Namespace, paths: AppPaths) -> int:
    file_path = _resolve_path(args.file)
    print(f"File: {file_path}")
    settings = load_settings(paths)
    database: FileRecord | None = None
    connection: sqlite3.Connection | None = None
    if paths.db_path.exists():
        connection = db.connect(paths.db_path, read_only=True)
        database = db.get_file_by_path(connection, file_path)
        try:
            print(f"Database mode: {_mode_label(settings)}")
            if database is None:
                print("Database: not indexed (xattrs shown below)")
            else:
                print(f"Database status: {database.status.value}")
                print(f"Database category: {database.category or '(none)'}")
                print(f"Database summary: {database.summary or '(none)'}")
                tag_rows = db.file_tags(connection, int(database.id))
                if tag_rows:
                    print("Database tags:")
                    for row in tag_rows:
                        confidence = (
                            f" confidence={row['confidence']:.2f}"
                            if row["confidence"] is not None
                            else ""
                        )
                        print(f"  - {row['name']} [{row['source']}]{confidence}")
                else:
                    print("Database tags: (none)")
        finally:
            connection.close()
    else:
        print("Database: not initialized")

    try:
        raw = list_smartfs_xattrs(file_path)
    except XattrUnsupported as exc:
        print(f"Raw xattrs: unsupported ({exc})")
    except XattrOperationError as exc:
        print(f"Raw xattrs: error ({exc})")
    else:
        if raw:
            print("Raw xattrs:")
            for name in sorted(raw):
                print(f"  {name}={raw[name]}")
        else:
            print("Raw xattrs: (none)")

    print(f"Verify with: getfattr -d -m user.smartfs {file_path}")
    return 0


def _cmd_tag_add(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    connection = _open_database(paths, settings)
    try:
        _path, record = _indexed_file(connection, args.file)
        if record.id is None:
            raise ConfigError("Indexed file has no id")
        result = add_tag(
            connection,
            int(record.id),
            args.tag,
            source=TagSource.USER,
            suppression_ttl=settings.self_write_suppression_seconds,
        )
    finally:
        connection.close()
    print(f"Tagged: {result.path}")
    print(f"Tags (union): {', '.join(result.tags) if result.tags else '(none)'}")
    if result.sync.supported:
        print("xattrs: updated")
    else:
        print(f"xattrs: DB-only ({result.sync.message})")
    print(f"Verify with: getfattr -d -m user.smartfs {result.path}")
    return 0


def _cmd_tag_remove(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    connection = _open_database(paths, settings)
    try:
        _path, record = _indexed_file(connection, args.file)
        if record.id is None:
            raise ConfigError("Indexed file has no id")
        source = None if args.source == "any" else TagSource(args.source)
        result = remove_tag(
            connection,
            int(record.id),
            args.tag,
            source=source,
            suppression_ttl=settings.self_write_suppression_seconds,
        )
    finally:
        connection.close()
    print(f"Removed tag from: {result.path}")
    print(f"Tags (union): {', '.join(result.tags) if result.tags else '(none)'}")
    if result.sync.supported:
        print("xattrs: updated")
    else:
        print(f"xattrs: DB-only ({result.sync.message})")
    return 0


def _explanation(
    hit: SearchHit,
    requested_tags: list[str],
    category: str | None,
    query: str,
    connection: sqlite3.Connection,
) -> str:
    record = hit.record
    parts: list[str] = []
    if requested_tags:
        parts.append("matched tag(s): " + ", ".join(f"'{tag}'" for tag in requested_tags))
    if category:
        parts.append(f"category: {category}")
    if query:
        parts.append(f"query: '{query}'")
    if hit.text_rank is not None:
        score = f", BM25 {hit.text_score:.3f}" if hit.text_score is not None else ""
        parts.append(f"full-text rank #{hit.text_rank}{score}")
    if hit.semantic_rank is not None:
        similarity = (
            f", cosine {hit.semantic_similarity:.3f}" if hit.semantic_similarity is not None else ""
        )
        parts.append(f"semantic rank #{hit.semantic_rank}{similarity}")
    if hit.text_rank is not None and hit.semantic_rank is not None:
        parts.append(f"RRF score {hit.rrf_score:.5f}")
    if hit.text_snippet:
        parts.append(f"matched text: {hit.text_snippet[:300]}")
    if record.id is not None:
        tags = db.tag_union(connection, int(record.id))
        if tags:
            parts.append("file tags: " + ", ".join(tags))
    if not parts:
        return "listed all indexed documents"
    return "; ".join(parts)


def _cmd_find(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    normalized_tags = [normalize_tag(tag) for tag in args.tag]
    category: str | None = None
    if args.category:
        try:
            category = Category(args.category.strip().lower()).value
        except ValueError as exc:
            allowed = ", ".join(item.value for item in Category)
            raise ConfigError(f"Unknown category {args.category!r}. Allowed: {allowed}") from exc
    query = " ".join(args.query).strip()
    if args.limit < 1:
        raise ConfigError("--limit must be positive")
    embedding_service = (
        EmbeddingService(settings, paths.cache_dir / "fastembed")
        if settings.embeddings_enabled and args.mode != SearchMode.TEXT.value
        else None
    )
    connection = _open_database(paths, settings)
    try:
        try:
            report = search_files(
                connection,
                settings,
                query,
                tags=normalized_tags,
                category=category,
                limit=args.limit,
                mode=args.mode,
                embedding_service=embedding_service,
            )
        except SearchError as exc:
            raise ConfigError(str(exc)) from exc
        if report.note:
            print(f"Search note: {report.note}", file=sys.stderr)
        if args.explain:
            print(
                f"Search mode: {report.used_mode} "
                f"(text={report.text_candidates}, semantic={report.semantic_candidates})"
            )
            if report.fts_query:
                print(f"FTS query: {report.fts_query}")
        if not report.hits:
            print("No matches.")
            return 0
        for hit in report.hits:
            record = hit.record
            tags = db.tag_union(connection, int(record.id)) if record.id else []
            tag_suffix = f" [{', '.join(tags)}]" if tags else ""
            category_suffix = f" ({record.category})" if record.category else ""
            print(f"{record.path}{category_suffix}{tag_suffix}")
            if args.explain:
                print("  why: " + _explanation(hit, normalized_tags, category, query, connection))
        return 0
    finally:
        connection.close()


def _cmd_status(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = load_settings(paths)
    print(f"SmartFS {__version__} (Phase 7)")
    print(f"Mode: {_mode_label(settings)}")
    print(f"Analysis: {analysis_label(settings)}")
    credential = credential_status()
    if credential.available:
        print(f"API key: configured ({credential.source})")
    else:
        print(f"API key: missing ({credential.detail or 'not configured'})")
    print(f"Embeddings: {embedding_label(settings)}")
    print(f"Config: {paths.config_file}")
    print(f"Database: {paths.db_path}")
    print(f"Logs: {paths.log_file}")
    print(f"xattr mirroring: {'enabled' if settings.xattr_enabled else 'disabled'}")
    print(f"Max file size: {settings.max_file_size_bytes} bytes")
    print(f"Exclusions: {len(settings.exclusions)} patterns")
    print("Background queue: persistent jobs (start with 'smartfs daemon')")
    if settings.watched_dirs:
        print("Watched directories:")
        for directory in settings.watched_dirs:
            if not os.path.isdir(directory):
                print(f"- {directory} (missing)")
                continue
            support = probe_xattr_support(directory)
            label = "xattr=yes" if support.supported else f"xattr=no ({support.reason})"
            print(f"- {directory} ({label})")
    else:
        print("Watched directories: (none)")

    if not paths.db_path.exists():
        print("Index: not initialized. Run 'smartfs init'.")
        return 0
    db.init_database(
        paths.db_path,
        backup_dir=paths.backup_dir,
        backup_keep=5,
    )
    connection = db.connect(paths.db_path, read_only=True)
    try:
        jobs = db.job_stats(connection)
        print(
            "Jobs: "
            f"total={jobs['total']} queued={jobs['queued']} running={jobs['running']} "
            f"retry_scheduled={jobs['retry_scheduled']} failed={jobs['failed']}"
        )
        recent_failures = connection.execute(
            "SELECT id, path, state, attempts, last_error FROM jobs "
            "WHERE state IN ('failed','retry_scheduled') ORDER BY id DESC LIMIT 5"
        ).fetchall()
        if recent_failures:
            print("Recent job failures:")
            for row in recent_failures:
                error = redact_secrets(row["last_error"] or "")
                print(
                    f"- #{row['id']} [{row['state']}] {row['path']} "
                    f"attempts={row['attempts']} error={error}"
                )
        counts = db.status_counts(connection)
        total = sum(counts.values())
        print(f"Indexed files: {total}")
        for status in FileStatus:
            print(f"- {status.value}: {counts.get(status.value, 0)}")
        failures = connection.execute(
            "SELECT COALESCE(error_kind, 'unknown') AS kind, COUNT(*) AS count "
            "FROM files WHERE status = 'failed' GROUP BY kind ORDER BY count DESC"
        ).fetchall()
        if failures:
            print("Failures:")
            for row in failures:
                print(f"- {row['kind']}: {row['count']}")
        tags_count = connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        links_count = connection.execute("SELECT COUNT(*) FROM file_tags").fetchone()[0]
        print(f"Tags: {tags_count} unique, {links_count} file-tag links")
        search_documents = connection.execute(
            "SELECT COUNT(*) FROM files_fts JOIN files f ON f.id = files_fts.rowid "
            "WHERE f.status = 'done'"
        ).fetchone()[0]
        print(f"Full-text index: {search_documents} documents")
        analysis_counts = db.analysis_stats(connection)
        if analysis_counts:
            summary = ", ".join(
                f"{name}={count}" for name, count in sorted(analysis_counts.items())
            )
            print(f"Analysis results: {summary}")
        embedding_counts = db.embedding_stats(connection)
        if embedding_counts.get("total", 0):
            summary = ", ".join(
                f"{name}={count}"
                for name, count in sorted(embedding_counts.items())
                if name != "total"
            )
            print(f"Embeddings: total={embedding_counts['total']} ({summary})")
        else:
            print("Embeddings: 0")
    finally:
        connection.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = AppPaths.discover()
    with suppress(OSError):
        configure_logging(paths, verbose=args.verbose)
    try:
        return int(args.handler(args, paths))
    except (
        ConfigError,
        CredentialError,
        AnalysisError,
        db.DatabaseError,
        ScanError,
        SearchError,
        RelatedError,
        TagError,
        XattrError,
    ) as exc:
        message = redact_secrets(str(exc))
        LOGGER.error("Command failed", extra={"event": "cli.error", "error": message})
        print(f"error: {message}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        message = redact_secrets(str(exc))
        LOGGER.error(
            "Database command failed",
            extra={"event": "cli.db_error", "error": message},
        )
        print(f"error: database failure: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        LOGGER.exception(
            "Unexpected command failure",
            extra={"event": "cli.unhandled", "error_type": type(exc).__name__},
        )
        print(
            f"error: unexpected {type(exc).__name__}: {redact_secrets(str(exc))}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
