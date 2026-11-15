import sys

from audiobookdl import args, logging
from audiobookdl.config import load_config
from audiobookdl.pipeline import Downloader, apply_runtime_options


def main() -> None:
    """CLI entry point for `audiobook-dl`."""
    options = args.parse_arguments()
    config = load_config(options.config_location)
    urls = args.get_urls(options)
    ctx = apply_runtime_options(options, config)
    logging.debug_mode = ctx.debug
    logging.quiet_mode = ctx.quiet
    logging.ffmpeg_output = ctx.ffmpeg_output
    if not urls:
        with logging.job_context(ctx):
            logging.simple_help()
        sys.exit()
    Downloader(config, options).download(urls)


if __name__ == "__main__":
    main()
