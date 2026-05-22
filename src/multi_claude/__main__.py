"""Entrypoint. `python -m multi_claude` and the `multi-claude` console script both land here."""

from __future__ import annotations


def main() -> None:
    from multi_claude.app import ClaudeBrowserApp

    ClaudeBrowserApp().run()


if __name__ == "__main__":
    main()
