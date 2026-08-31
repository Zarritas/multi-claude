"""Filter query parsing and matching shared by the projects/sessions screens.

A query looks like::

    branch:main feature  → branch == "main" AND fuzzy("feature") on the rest
    refacto              → fuzzy("refacto") across all searchable fields

Supported keys (where present):
- ``branch:`` — substring match against the branch field
- ``path:``   — substring match against the project path
- ``id:``     — substring match against the session id
- ``tag:``    — comma-separated list, every item must match a session tag
- ``file:``   — a file the session edited (basename, or the path if it has a separator)
- ``author:`` — substring match against who published a session
- ``secrets:`` — ``yes`` / ``no`` / ``unknown``, against the credential scan's verdict

Free-text terms are scored with :func:`rapidfuzz.fuzz.partial_ratio`. A match
requires score >= :data:`FUZZY_THRESHOLD`.

A key that a screen's rows cannot answer filters everything out rather than being
ignored: ``author:`` over the projects list has no meaning, and showing every project
would read as "none of these has an author" when it means "the question did not apply".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from multi_claude.index import basename

FUZZY_THRESHOLD = 70

KNOWN_KEYS: frozenset[str] = frozenset({"branch", "path", "id", "tag", "file", "author", "secrets"})

# What ``secrets:`` accepts, in both languages, mapped onto the three answers the scan can
# give. "unknown" is a real answer and not a synonym of "no": a session nobody has scanned
# yet is not a session that came back clean, and collapsing the two would turn the filter
# into a claim it cannot make.
SECRETS_VALUES: dict[str, str] = {
    "yes": "yes",
    "si": "yes",
    "sí": "yes",
    "true": "yes",
    "1": "yes",
    "no": "no",
    "false": "no",
    "0": "no",
    "clean": "no",
    "limpias": "no",
    "unknown": "unknown",
    "desconocido": "unknown",
    "?": "unknown",
}


def file_matches(term: str, paths: tuple[str, ...]) -> bool:
    """Whether any of ``paths`` is the file ``term`` is asking about.

    A term with a separator in it is compared against the whole path, one without against
    the basename. That is how the question gets asked: `index.py` when you mean the file,
    `multi_claude/index.py` when you mean *that* one and not another index.py in the tree.
    Both are substring matches, so a fragment works and a trailing `.py` is not required.

    Mirrors :meth:`SessionIndex.sessions_touching`, which asks the same question in SQL.
    Any divergence between the two shows up as the listing and the global search
    disagreeing about the same session, so they are tested against each other.
    """
    term = term.strip().lower().replace("\\", "/")
    if not term:
        return False
    if "/" in term:
        return any(term in path.replace("\\", "/").lower() for path in paths)
    return any(term in basename(path) for path in paths)


def secrets_wanted(value: str) -> str | None:
    """Normalise a ``secrets:`` value, or None if it is not one we understand.

    A caller that gets None must let nothing through rather than ignore the constraint:
    silently dropping ``secrets:puede`` would answer a question nobody asked.
    """
    return SECRETS_VALUES.get(value.strip().lower())


def secrets_verdict(count: int | None) -> str:
    """The scan's answer for a session, as ``secrets:`` spells it.

    ``None`` means never scanned, which is why this is not just ``bool(count)``.
    """
    if count is None:
        return "unknown"
    return "yes" if count else "no"


@dataclass(frozen=True)
class FilterQuery:
    free_text: str = ""
    constraints: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.free_text and not self.constraints


def parse_query(raw: str) -> FilterQuery:
    """Split ``raw`` into ``key:value`` constraints + free-text remainder."""
    tokens = raw.strip().split()
    free: list[str] = []
    constraints: dict[str, str] = {}
    for token in tokens:
        if ":" in token:
            key, _, value = token.partition(":")
            key = key.lower()
            if key in KNOWN_KEYS and value:
                constraints[key] = value.lower()
                continue
        free.append(token)
    return FilterQuery(free_text=" ".join(free), constraints=constraints)


def matches_fuzzy(haystack: str, free_text: str) -> bool:
    """``True`` iff ``haystack`` matches ``free_text`` (substring or partial fuzz)."""
    if not free_text:
        return True
    haystack_l = haystack.lower()
    ft_l = free_text.lower()
    if ft_l in haystack_l:
        return True
    score = float(fuzz.partial_ratio(ft_l, haystack_l))
    return score >= FUZZY_THRESHOLD
