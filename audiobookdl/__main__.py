import sys
import requests

from audiobookdl import __version__, args, logging
from audiobookdl.config import load_config
from audiobookdl.pipeline import (
    Downloader,
    apply_runtime_options,
    list_service_library,
    search_service,
    verify_service_auth,
)
from audiobookdl.sources import get_available_services
from audiobookdl.utils.updater import check_for_updates


def main() -> None:
    """CLI entry point for `audiobook-dl`."""
    options = args.parse_arguments()
    if getattr(options, "list_sources", False):
        for service in get_available_services():
            methods = ", ".join(service.auth_methods) if service.auth_methods else "none"
            print(f"{service.key:<20} {service.label} (auth: {methods})")
        return
    config = load_config(options.config_location)
    ctx = apply_runtime_options(options, config)
    logging.debug_mode = ctx.debug
    logging.quiet_mode = ctx.quiet
    logging.ffmpeg_output = ctx.ffmpeg_output

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
        print(f"Authentication for '{target_service}': {'OK' if ok else 'FAILED'}")
        sys.exit(0 if ok else 1)

    if getattr(options, "search_query", None):
        target_service = getattr(options, "service", None) or "librivox"
        items = search_service(target_service, options.search_query, options, config)
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
        for item in items:
            author = f" — {item.author}" if item.author else ""
            print(f"{item.title}{author}\n  {item.url}")
        return

    urls = args.get_urls(options)
    if not urls:
        with logging.job_context(ctx):
            logging.simple_help()
        sys.exit()
    summary = Downloader(config, options).download(urls)
    if summary.cancelled:
        sys.exit(130)
    if summary.failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
