"""Filter query parsing and matching shared by the projects/sessions screens.

A query looks like::

    branch:main feature  → branch == "main" AND fuzzy("feature") on the rest
    refacto              → fuzzy("refacto") across all searchable fields

Supported keys (where present):
- ``branch:`` — substring match against the branch field
- ``path:``   — substring match against the project path
- ``id:``     — substring match against the session id
- ``tag:``    — comma-separated list, every item must match a session tag
- ``author:`` — substring match against who published a session

Free-text terms are scored with :func:`rapidfuzz.fuzz.partial_ratio`. A match
requires score >= :data:`FUZZY_THRESHOLD`.

A key that a screen's rows cannot answer filters everything out rather than being
ignored: ``author:`` over the projects list has no meaning, and showing every project
would read as "none of these has an author" when it means "the question did not apply".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

FUZZY_THRESHOLD = 70

KNOWN_KEYS: frozenset[str] = frozenset({"branch", "path", "id", "tag", "author"})


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
