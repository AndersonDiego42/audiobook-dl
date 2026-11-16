import sys
from typing import Optional, Sequence
import requests

from audiobookdl import __version__, args, logging
from audiobookdl.config import load_config
from audiobookdl.pack import PackOverrides, run_pack
from audiobookdl.pipeline import (
    Downloader,
    apply_runtime_options,
    list_service_library,
    search_service,
    verify_service_auth,
)
from audiobookdl.sources import get_available_services
from audiobookdl.utils.archive import DownloadArchive, default_archive_path
from audiobookdl.utils.updater import check_for_updates


def _build_pack_overrides(pack_opts: args.PackOptions) -> Optional[PackOverrides]:
    if not any((
        pack_opts.title,
        pack_opts.authors,
        pack_opts.narrators,
        pack_opts.series is not None,
        pack_opts.series_order is not None,
        pack_opts.isbn is not None,
        pack_opts.asin is not None,
        pack_opts.cover_path,
    )):
        return None
    return PackOverrides(
        title=pack_opts.title,
        authors=pack_opts.authors,
        narrators=pack_opts.narrators,
        series=pack_opts.series,
        series_order=pack_opts.series_order,
        isbn=pack_opts.isbn,
        asin=pack_opts.asin,
        cover_path=pack_opts.cover_path,
    )


def _resolve_archive_path(config_location: Optional[str], archive_path: Optional[str]) -> str:
    cfg = load_config(config_location)
    db_dir = cfg.database_directory or args.default_database_directory()
    return archive_path or cfg.download_archive or default_archive_path(db_dir)


def _run_library_command(lib_opts: args.LibraryOptions) -> int:
    """Execute `audiobook-dl library` subcommand actions against `downloads.db`."""
    archive_path = _resolve_archive_path(lib_opts.config_location, lib_opts.archive_path)
    archive = DownloadArchive(archive_path)

    if lib_opts.action == "list":
        entries = archive.list_entries(
            query=lib_opts.query,
            limit=lib_opts.limit,
            include_trashed=lib_opts.include_trashed,
            include_hidden=lib_opts.include_hidden,
        )
        if getattr(lib_opts, "json_output", False):
            import json
            from dataclasses import asdict

            print(json.dumps([asdict(entry) for entry in entries], indent=2, ensure_ascii=False))
            return 0
        for entry in entries:
            tags_str = f" [{' '.join(entry.tags)}]" if entry.tags else ""
            author_str = f" — {entry.author}" if entry.author else ""
            print(f"[{entry.id}] ({entry.status}) {entry.source}: {entry.title}{author_str}{tags_str}")
        return 0

    for entry_id in lib_opts.entry_ids:
        entry = archive.get_entry(entry_id)
        if entry is None:
            print(f"Error: library entry {entry_id} not found", file=sys.stderr)
            return 1
        if lib_opts.action == "tag":
            to_add = [t for t in lib_opts.tags if not t.startswith("-")]
            to_remove = [t[1:] for t in lib_opts.tags if t.startswith("-")]
            if to_add:
                archive.add_tags(entry_id, to_add)
            if to_remove:
                archive.remove_tags(entry_id, to_remove)
        elif lib_opts.action == "hide":
            archive.add_tags(entry_id, ["hidden"])
        elif lib_opts.action == "unhide":
            archive.remove_tags(entry_id, ["hidden"])
        elif lib_opts.action == "trash":
            archive.set_status(entry_id, "trashed")
        elif lib_opts.action == "restore":
            restored = "downloaded" if entry.output_path else "pending"
            archive.set_status(entry_id, restored)
    return 0


def _run_scan_command(scan_opts: args.ScanOptions) -> int:
    """Execute `audiobook-dl scan` against local library folders."""
    import json
    from .utils.library_scanner import scan_local_library

    archive_path = _resolve_archive_path(scan_opts.config_location, scan_opts.archive_path)
    archive = DownloadArchive(archive_path)
    result = scan_local_library(
        scan_opts.paths,
        archive,
        profile=scan_opts.profile,
        verify_missing=scan_opts.verify_missing,
        dry_run=scan_opts.dry_run,
    )
    if scan_opts.json_output:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        for item in result.items:
            author = f" - {item.author}" if item.author else ""
            print(f"[{item.action.upper()}] {item.title}{author} -> {item.output_path}")
        for err in result.errors:
            print(f"Warning: {err}", file=sys.stderr)
        print(
            f"Scanned {result.scanned_books} book(s): "
            f"{result.matched} matched, {result.registered} newly registered, "
            f"{result.missing} missing on disk."
        )
    return 1 if result.errors and result.scanned_books == 0 else 0


def _run_export_command(export_opts: args.ExportOptions) -> int:
    """Execute `audiobook-dl export` against `downloads.db`."""
    from .utils.export import export_library_entries

    archive_path = _resolve_archive_path(export_opts.config_location, export_opts.archive_path)
    archive = DownloadArchive(archive_path)
    entries = archive.list_entries(
        query=export_opts.query,
        limit=export_opts.limit,
        include_trashed=export_opts.include_trashed,
        include_hidden=export_opts.include_hidden,
    )
    count = export_library_entries(entries, export_opts.output_path, fmt=export_opts.fmt)
    print(f"Exported {count} book(s) to {export_opts.output_path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    """CLI entry point for `audiobook-dl`."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "library":
        lib_opts = args.parse_library_arguments(raw_argv[1:])
        sys.exit(_run_library_command(lib_opts))
    if raw_argv and raw_argv[0] == "scan":
        scan_opts = args.parse_scan_arguments(raw_argv[1:])
        sys.exit(_run_scan_command(scan_opts))
    if raw_argv and raw_argv[0] == "export":
        export_opts = args.parse_export_arguments(raw_argv[1:])
        sys.exit(_run_export_command(export_opts))
    if raw_argv and raw_argv[0] == "sync":
        from .sync import run_sync_loop

        sync_opts = args.parse_sync_arguments(raw_argv[1:])
        sys.exit(run_sync_loop(sync_opts))
    if raw_argv and raw_argv[0] == "pack":
        pack_dl_options, pack_options = args.parse_pack_arguments(raw_argv[1:])
        config = load_config(pack_dl_options.config_location)
        summary = run_pack(
            pack_options.paths,
            pack_dl_options,
            config,
            output_dir=pack_options.output_dir,
            recursive=pack_options.recursive,
            delete_source=pack_options.delete_source,
            dry_run=pack_options.dry_run,
            overrides=_build_pack_overrides(pack_options),
        )
        if getattr(pack_options, "json_output", False):
            import json

            print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
        if summary.cancelled:
            sys.exit(130)
        if summary.books_failed > 0:
            sys.exit(1)
        return

    options = args.parse_arguments(raw_argv)
    if getattr(options, "list_sources", False):
        services = get_available_services()
        if getattr(options, "json_output", False):
            import json
            from dataclasses import asdict

            print(json.dumps([asdict(s) for s in services], indent=2, ensure_ascii=False))
            return
        for service in services:
            methods = ", ".join(service.auth_methods) if service.auth_methods else "none"
            print(f"{service.key:<20} {service.label} (auth: {methods})")
        return
    config = load_config(options.config_location)
    ctx = apply_runtime_options(options, config)

    with logging.job_context(ctx):
        if getattr(options, "check_updates", False):
            with requests.Session() as update_session:
                info = check_for_updates(
                    proxy=options.proxy,
                    session=update_session,
                    respect_opt_out=False,
                )
            if info is None:
                print(f"Could not check for updates (current version: v{__version__}).", file=sys.stderr)
                sys.exit(1)
            if info.has_update:
                print(f"Update available: v{info.latest_version} (current: v{info.current_version})")
                print(f"Download: {info.release_url}")
            else:
                print(f"Up to date (v{info.current_version})")
            return

        if getattr(options, "verify_auth", False):
            target_service = getattr(options, "service", None)
            if not target_service:
                print("Error: --verify-auth requires --service <name>", file=sys.stderr)
                sys.exit(2)
            ok = verify_service_auth(target_service, options, config)
            if getattr(options, "json_output", False):
                import json

                print(json.dumps({"service": target_service, "authenticated": bool(ok)}, indent=2))
            else:
                print(f"Authentication for '{target_service}': {'OK' if ok else 'FAILED'}")
            sys.exit(0 if ok else 1)

        if getattr(options, "search_query", None):
            target_service = getattr(options, "service", None) or "librivox"
            items = search_service(target_service, options.search_query, options, config)
            if getattr(options, "json_output", False):
                import json
                from dataclasses import asdict

                print(json.dumps([asdict(item) for item in items], indent=2, ensure_ascii=False))
                return
            for item in items:
                author = f" — {item.author}" if item.author else ""
                print(f"{item.title}{author}\n  {item.url}")
            return

        if getattr(options, "library_list", False):
            target_service = getattr(options, "service", None)
            if not target_service:
                print("Error: --library-list requires --service <name>", file=sys.stderr)
                sys.exit(2)
            items = list_service_library(target_service, options, config)
            if getattr(options, "json_output", False):
                import json
                from dataclasses import asdict

                print(json.dumps([asdict(item) for item in items], indent=2, ensure_ascii=False))
                return
            for item in items:
                author = f" — {item.author}" if item.author else ""
                print(f"{item.title}{author}\n  {item.url}")
            return

        urls = args.get_urls(options)
        if not urls:
            logging.simple_help()
            sys.exit()
        summary = Downloader(config, options).download(urls)
        if getattr(options, "json_output", False):
            import json

            print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
        if summary.cancelled:
            sys.exit(130)
        if summary.failed > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
