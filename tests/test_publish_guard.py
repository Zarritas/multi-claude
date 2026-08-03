"""Tests for the check that stops a publish from overwriting someone else's work.

The shape is a git fast-forward: safe when the remote still carries the stamp our copy came
from, a decision when it does not. What matters most here is the direction of the errors —
failing to warn loses a colleague's turns, warning too eagerly just costs a dialogue.
"""

from __future__ import annotations

from multi_claude.publish_guard import Conflict, find_conflict
from multi_claude.remote import RemoteSession

MINE = "yo@example.com"
THEIRS = "ana@example.com"


def _published(
    *, at: str = "2026-08-01T10:00:00+00:00", by: str | None = THEIRS, messages: int = 455
) -> RemoteSession:
    return RemoteSession(
        session_id="ses-1",
        published_at=at,
        published_by=by,
        message_count=messages,
    )


def _check(**kwargs: object) -> Conflict | None:
    args: dict[str, object] = {
        "session_id": "ses-1",
        "local_messages": 412,
        "remote": _published(),
        "base": "2026-08-01T10:00:00+00:00",
        "own_email": MINE,
    }
    args.update(kwargs)
    return find_conflict(**args)  # type: ignore[arg-type]


# --- safe to publish -----------------------------------------------------------------


def test_publishing_something_new_is_safe() -> None:
    assert _check(remote=None, base=None) is None


def test_a_fast_forward_is_safe() -> None:
    """The remote still carries the stamp our copy derives from: nobody touched it."""
    assert _check(base="2026-08-01T10:00:00+00:00") is None


def test_republishing_your_own_session_without_a_base_is_safe() -> None:
    """A machine that published before bases were recorded. The only history at risk is ours."""
    assert _check(remote=_published(by=MINE), base=None) is None


def test_the_email_comparison_ignores_case_and_spaces() -> None:
    assert _check(remote=_published(by=" YO@Example.com "), base=None) is None


# --- not safe ------------------------------------------------------------------------


def test_a_different_stamp_is_a_conflict() -> None:
    """Someone published on top since our copy came from the remote."""
    conflict = _check(base="2026-07-30T09:00:00+00:00")
    assert conflict is not None
    assert conflict.session_id == "ses-1"
    assert conflict.published_by == THEIRS


def test_someone_elses_session_with_no_base_is_a_conflict() -> None:
    """We never fetched it, so we cannot claim to derive from it."""
    assert _check(base=None) is not None


def test_a_missing_stamp_on_the_remote_still_conflicts() -> None:
    """An empty published_at is not the base we recorded, so it is not a fast-forward."""
    assert _check(remote=_published(at=""), base="2026-08-01T10:00:00+00:00") is not None


def test_size_is_never_the_signal() -> None:
    """A jsonl only grows: "mine is bigger" is true whether or not theirs grew too."""
    bigger_local = _check(local_messages=9999, base="2026-07-30T09:00:00+00:00")
    smaller_local = _check(local_messages=1, base="2026-07-30T09:00:00+00:00")
    assert bigger_local is not None
    assert smaller_local is not None


# --- what the dialogue shows ---------------------------------------------------------


def test_describe_puts_both_sides_and_no_email_domain() -> None:
    conflict = _check(base="2026-07-30T09:00:00+00:00")
    assert conflict is not None
    line = conflict.describe()
    assert "412 mensajes" in line  # mine
    assert "455 mensajes" in line  # theirs
    assert "ana" in line
    assert "@example.com" not in line  # the local part is enough to know who


def test_describe_survives_missing_counts_and_author() -> None:
    conflict = _check(remote=_published(by=None, messages=0), local_messages=0, base="otra-cosa")
    assert conflict is not None
    assert "alguien" in conflict.describe()
