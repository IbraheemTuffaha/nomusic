"""Compatibility launcher for ``python -m tools.cli URL`` from backend/."""

from nomusic.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
