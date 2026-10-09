"""Compatibility launcher; the installed entry point is ``nomusic serve``."""

from nomusic.server import main


if __name__ == "__main__":
    main()
