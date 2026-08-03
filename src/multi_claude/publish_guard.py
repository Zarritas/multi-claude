"""Decide whether publishing a session would overwrite someone else's work.

Republishing is the one operation in the shared-session flow that can *lose* data: the
remote keeps one manifest per session id, so publishing a session that a colleague has
published on top replaces theirs. Over SSH git refuses the push and the retry lands on
top of theirs, but the REST backends have no such thing — the second writer simply wins.

The check is the same shape as a git fast-forward. Each machine records which published
version its copy derives from (:meth:`SessionIndex.record_publish_base`); if the remote's
manifest still carries that stamp, nothing happened since and publishing is safe. If it
carries a different one, the histories diverged and publishing is a decision, not a
routine — which is why this module only ever *reports*, and never writes.

Why not compare sizes: a jsonl only grows, so "mine is bigger than the published one" is
equally true when the other person changed nothing and when they added a hundred turns
after I fetched it. The stamp distinguishes them; a size cannot.
"""

from __future__ import annotations

from dataclasses import dataclass

from multi_claude.remote import RemoteSession


@dataclass(frozen=True)
class Conflict:
    """A session whose published version is not the one the local copy derives from."""

    session_id: str
    remote: RemoteSession
    base: str | None
    local_messages: int

    @property
    def published_by(self) -> str:
        return self.remote.published_by or "alguien"

    def describe(self) -> str:
        """One line for the dialogue: who has what, so the choice is informed."""
        theirs = f"{self.remote.message_count} mensajes" if self.remote.message_count else "?"
        mine = f"{self.local_messages} mensajes" if self.local_messages else "?"
        who = self.published_by.split("@")[0]
        return f"{self.session_id[:8]} · la tuya {mine} · la de {who} {theirs}"


def find_conflict(
    *,
    session_id: str,
    local_messages: int,
    remote: RemoteSession | None,
    base: str | None,
    own_email: str | None,
) -> Conflict | None:
    """Whether publishing this session would overwrite a version we did not start from.

    ``None`` means go ahead. The three ways to be safe:

    - **it is not published yet** — nothing to overwrite;
    - **the remote still carries the stamp we derive from** — a fast-forward;
    - **we published it ourselves** and have no recorded base — a machine that published
      before bases were recorded, republishing its own session. Blocking that would be
      friction with no one to protect: the only history at risk is our own.
    """
    if remote is None:
        return None
    if base is not None and base == (remote.published_at or ""):
        return None
    if base is None and _same_person(own_email, remote.published_by):
        return None
    return Conflict(
        session_id=session_id,
        remote=remote,
        base=base,
        local_messages=local_messages,
    )


def _same_person(own_email: str | None, published_by: str | None) -> bool:
    if not own_email or not published_by:
        return False
    return own_email.strip().lower() == published_by.strip().lower()
