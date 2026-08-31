#!/usr/bin/env python3
"""Extract one version's body from the CHANGELOG, to use as a GitHub release's notes.

It lives here rather than inside the package because it is not part of the product: the
release workflow runs it, and so does whoever wants to see what is about to be published
before pushing the tag.

**Failing** when the section is missing is the point, not a side effect: the `v1.0.0` tag
and the `## [1.0.0]` heading are the same claim written twice, and the usual way to break
it is tagging without having closed `[Unreleased]` first. The workflow calls this before
building anything, so that slip stops the release instead of publishing a version whose
notes are the previous one's — or empty.

    python tools/release_notes.py 1.0.0
    python tools/release_notes.py v1.0.0 --changelog CHANGELOG.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# "## [1.0.0] - 2026-08-31", which is what Keep a Changelog writes. The date is not
# captured: whoever reads the release already has it on the release itself.
_HEADING = re.compile(r"^##\s+\[(?P<version>[^\]]+)\]")

# The reference-link definitions that close the file ("[1.0.0]: https://…"). They belong to
# the document, not to any one version, and in a release body they would only be noise.
_LINK_DEF = re.compile(r"^\[[^\]]+\]:\s+\S+")


def section(changelog: str, version: str) -> str | None:
    """The body of ``version``'s section, or None if the CHANGELOG has no such section.

    The body runs from the version's heading to the next level-2 heading (or the end of the
    file), without the heading itself and without the link definitions.
    """
    wanted = version.removeprefix("v")
    body: list[str] | None = None

    for line in changelog.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if body is not None:
                break  # the previous version starts here, so ours ends here
            if heading.group("version").removeprefix("v") == wanted:
                body = []
            continue
        if body is not None and not _LINK_DEF.match(line):
            body.append(line)

    if body is None:
        return None
    return "\n".join(body).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_notes.py",
        description="Imprime el cuerpo del CHANGELOG para una versión.",
    )
    parser.add_argument("version", help="la versión a extraer, con o sin la 'v' del tag")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "CHANGELOG.md",
        help="ruta del CHANGELOG (por defecto, el de la raíz del repo)",
    )
    args = parser.parse_args(argv)

    try:
        changelog = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"no se pudo leer {args.changelog}: {exc}", file=sys.stderr)
        return 2

    body = section(changelog, args.version)
    if body is None:
        version = args.version.removeprefix("v")
        print(
            f"{args.changelog} no tiene una sección para {args.version}. "
            f"Cierra el bloque [Unreleased] como '## [{version}] - <fecha>' antes de etiquetar.",
            file=sys.stderr,
        )
        return 1

    if not body:
        print(f"la sección de {args.version} está vacía", file=sys.stderr)
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
