"""Installed command entry point; help and setup commands do not start a server."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Keep the existing pipeline parser responsible for its own options/help.
    if args and args[0] == "process":
        from .cli import main as process
        return process(args[1:])

    parser = argparse.ArgumentParser(prog="nomusic", description="Local no-music backend")
    parser.add_argument("--version", action="version", version=f"nomusic {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Start the local API")
    commands.add_parser("process", help="Process a source URL without the API")
    commands.add_parser("check-runtime", help="Check FFmpeg and the YouTube JavaScript runtime")
    models = commands.add_parser("models", help="Manage pinned model artifacts")
    model_commands = models.add_subparsers(dest="model_command", required=True)
    fetch = model_commands.add_parser("fetch", help="Download and verify the model files")
    fetch.add_argument("--model", choices=("htdemucs", "htdemucs_ft"), default="htdemucs")
    options = parser.parse_args(args)
    try:
        if options.command == "serve":
            from .server import main as serve
            serve()
        elif options.command == "check-runtime":
            from .runtime import check_runtime
            print(json.dumps(check_runtime(), indent=2))
        elif options.command == "models":
            from .engines.model_store import MODEL_RELEASES, fetch_model_files
            files = fetch_model_files(options.model)
            release = MODEL_RELEASES[options.model]
            print(json.dumps({
                "model": options.model, "repository": release.repo_id,
                "revision": release.revision,
                "files": {name: str(path) for name, path in files.items()},
                "sha256_verified": True,
            }, indent=2))
    except (RuntimeError, ValueError, OSError) as error:
        print(f"nomusic: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
