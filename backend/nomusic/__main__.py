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
    doctor_parser = commands.add_parser("doctor", help="Check the local installation and run offline test inference")
    doctor_parser.add_argument("--model", choices=("htdemucs", "htdemucs_ft"))
    doctor_parser.add_argument("--json", action="store_true", help="Print a structured report")
    doctor_parser.add_argument("--skip-inference", action="store_true", help="Check prerequisites only; does not verify processing")
    models = commands.add_parser("models", help="Manage pinned model artifacts")
    model_commands = models.add_subparsers(dest="model_command", required=True)
    fetch = model_commands.add_parser("fetch", help="Download and verify the model files")
    fetch.add_argument("--model", choices=("htdemucs", "htdemucs_ft"), default="htdemucs")
    auth = commands.add_parser("auth", help="Generate and revoke local operator keys")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    generate = auth_commands.add_parser("generate", help="Create a new operator key")
    generate.add_argument("--label", default=None, help="Optional operator/device label")
    generate.add_argument("--json", action="store_true", help="Print machine-readable output")
    auth_list = auth_commands.add_parser("list", help="List key ids without revealing keys")
    auth_list.add_argument("--json", action="store_true", help="Print machine-readable output")
    revoke = auth_commands.add_parser("revoke", help="Revoke a key by its id")
    revoke.add_argument("key_id")
    rotate = auth_commands.add_parser("rotate", help="Create a key and optionally revoke an old one")
    rotate.add_argument("--label", default=None, help="Optional operator/device label")
    rotate.add_argument("--revoke", dest="revoke_id", default=None, help="Key id to revoke after creating the replacement")
    rotate.add_argument("--json", action="store_true", help="Print machine-readable output")
    cache = commands.add_parser("cache", help="Administer the local media cache")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_commands.add_parser("stats", help="Show local storage usage")
    # These commands never travel over the application API.
    cache_commands.add_parser("clear", help="Delete unleased media; stop the server first for a full clear")
    options = parser.parse_args(args)
    try:
        if options.command == "serve":
            from .server import main as serve
            serve()
        elif options.command == "check-runtime":
            from .runtime import check_runtime
            print(json.dumps(check_runtime(), indent=2))
        elif options.command == "doctor":
            from .config import SETTINGS
            from .diagnostics import doctor, format_report
            report = doctor(SETTINGS, model=options.model, skip_inference=options.skip_inference)
            print(json.dumps(report, indent=2) if options.json else format_report(report))
            return 0 if report["ok"] else 1
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
        elif options.command == "cache":
            from .config import SETTINGS
            from .pipeline.cache import JobCache
            owner = JobCache(SETTINGS.cache_dir)
            try:
                if options.cache_command == "stats":
                    print(json.dumps(owner.stats(), indent=2))
                else:
                    print(json.dumps({"deleted_bytes": owner.clear_all()}))
            finally:
                owner.scratch.close()
        elif options.command == "auth":
            from .auth import AuthStore
            from .config import SETTINGS

            store = AuthStore(SETTINGS.auth_file, required=False)
            if options.auth_command == "generate":
                raw, info = store.generate(options.label)
                payload = {
                    "key": raw,
                    "key_id": info.key_id,
                    "label": info.label,
                    "created_at": info.created_at,
                    "warning": "The key is shown once; store it in the trusted extension settings.",
                }
                if options.json:
                    print(json.dumps(payload, indent=2))
                else:
                    print("Generated operator key (shown once):")
                    print(raw)
                    print(f"Key id: {info.key_id}")
                    print("Store it in the extension's trusted backend settings.")
            elif options.auth_command == "list":
                entries = [
                    {
                        "key_id": info.key_id,
                        "label": info.label,
                        "created_at": info.created_at,
                        "revoked_at": info.revoked_at,
                        "active": not info.revoked,
                    }
                    for info in store.list_keys()
                ]
                print(json.dumps(entries, indent=2))
            elif options.auth_command == "revoke":
                info = store.revoke(options.key_id)
                print(f"Revoked operator key {info.key_id}.")
            elif options.auth_command == "rotate":
                raw, info = store.rotate(label=options.label, revoke_id=options.revoke_id)
                payload = {
                    "key": raw,
                    "key_id": info.key_id,
                    "label": info.label,
                    "created_at": info.created_at,
                    "warning": "The key is shown once; store it in the trusted extension settings.",
                }
                if options.json:
                    print(json.dumps(payload, indent=2))
                else:
                    print("Generated replacement operator key (shown once):")
                    print(raw)
                    print(f"Key id: {info.key_id}")
            else:
                raise ValueError("unknown auth command")
    except (RuntimeError, ValueError, OSError) as error:
        print(f"nomusic: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
