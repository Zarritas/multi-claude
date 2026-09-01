"""Entrypoint. `python -m multi_claude` and the `multi-claude` console script both land here.

With no arguments it starts the TUI, which is what it has always done. The flags are for
the things that make more sense as a one-shot report than as a screen — and that are worth
being scriptable.
"""

from __future__ import annotations

import argparse
import sys

from multi_claude import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-claude",
        description="Navega y reanuda las sesiones de Claude Code. Sin argumentos, abre la TUI.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"multi-claude {__version__}",
        help=(
            "imprime la versión instalada y sale. La necesita cualquiera que reporte un "
            "fallo, y distingue una release de un checkout (que se identifica como "
            "'1.0.0.dev3+g<commit>')"
        ),
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
        "--stats",
        action="store_true",
        help=(
            "resume tiempo activo y tokens por proyecto y sale. El tiempo descuenta los "
            "huecos de más de 5 minutos; los tokens van separados porque los de caché son "
            "de otro orden. No da una cifra en euros a propósito: depende del modelo y del "
            "plan, y una que no cuadre con la factura es peor que ninguna."
        ),
    )
    parser.add_argument(
        "--since",
        metavar="AAAA-MM-DD",
        help="con --stats, cuenta solo las sesiones con actividad desde esa fecha",
    )
    parser.add_argument(
        "--project",
        metavar="RUTA",
        help=("con --audit-secrets o --stats, limita a ese proyecto y lo que hay debajo"),
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

    if args.stats:
        # Aliased: audit.py exports a format_report too, and both land in this function.
        from multi_claude.usage_report import build_report
        from multi_claude.usage_report import format_report as format_usage

        print(format_usage(build_report(since=args.since or "", only_path=args.project)))
        return 0

    if args.since:
        print("--since solo vale con --stats", file=sys.stderr)
        return 2

    if args.project or args.verbose:
        print("--project y --verbose solo valen con --audit-secrets o --stats", file=sys.stderr)
        return 2

    from multi_claude.app import ClaudeBrowserApp

    ClaudeBrowserApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
