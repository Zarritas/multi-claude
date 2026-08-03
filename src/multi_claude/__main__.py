"""Entrypoint. `python -m multi_claude` and the `multi-claude` console script both land here.

With no arguments it starts the TUI, which is what it has always done. The flags are for
the things that make more sense as a one-shot report than as a screen — and that are worth
being scriptable.
"""

from __future__ import annotations

import argparse
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-claude",
        description="Navega y reanuda las sesiones de Claude Code. Sin argumentos, abre la TUI.",
    )
    parser.add_argument(
        "--audit-secrets",
        action="store_true",
        help=(
            "revisa TODO el histórico buscando credenciales y sale. El escáner que corre "
            "al publicar, pero sobre todas las sesiones: una clave pegada en una "
            "conversación ya está en claro en el disco, se publique o no."
        ),
    )
    parser.add_argument(
        "--project",
        metavar="RUTA",
        help="con --audit-secrets, limita la revisión a ese proyecto y lo que hay debajo",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="con --audit-secrets, incluye un fragmento recortado de cada hallazgo",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.audit_secrets:
        from multi_claude.audit import audit, format_report

        report = audit(only_path=args.project)
        print(format_report(report, verbose=args.verbose))
        # Exit 1 when something was found, so a shell or a hook can act on it.
        return 1 if report.affected else 0

    if args.project or args.verbose:
        print("--project y --verbose solo valen con --audit-secrets", file=sys.stderr)
        return 2

    from multi_claude.app import ClaudeBrowserApp

    ClaudeBrowserApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
