"""Discover Claude projects on disk.

Scans `~/.claude/projects/`, resolves each encoded-path back to a real cwd
(reading the first jsonl event as source of truth), and flags orphans whose
real directory no longer exists.

For each live project we also resolve ``git_common_dir`` (worktree-aware) so
the UI can group multiple worktrees of the same repo together.
"""

from __future__ import annotations

import json
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CWD_SCAN_LINES = 80


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    encoded_path: Path
    session_count: int
    last_activity: float
    is_orphan: bool
    git_common_dir: Path | None = None


def scan_projects(projects_dir: Path | None = None) -> list[Project]:
    """Return all projects sorted by last_activity desc."""
    if projects_dir is None:
        projects_dir = CLAUDE_PROJECTS_DIR
    if not projects_dir.is_dir():
        return []
    projects: list[Project] = []
    for entry in projects_dir.iterdir():
        if not entry.is_dir():
            continue
        jsonl_files = list(entry.glob("*.jsonl"))
        if not jsonl_files:
            continue
        real_cwd = resolve_real_cwd(entry) or decode_path_fallback(entry.name)
        last_activity = max(f.stat().st_mtime for f in jsonl_files)
        is_orphan = not real_cwd.is_dir()
        name = real_cwd.name or str(real_cwd)
        common = None if is_orphan else resolve_git_common_dir(real_cwd)
        projects.append(
            Project(
                name=name,
                path=real_cwd,
                encoded_path=entry,
                session_count=len(jsonl_files),
                last_activity=last_activity,
                is_orphan=is_orphan,
                git_common_dir=common,
            )
        )
    projects.sort(key=lambda p: p.last_activity, reverse=True)
    return projects


def decode_path_fallback(encoded: str) -> Path:
    """Naive heuristic: every `-` becomes `/`. Used when no jsonl yields a cwd."""
    return Path("/" + encoded.lstrip("-").replace("-", "/"))


def resolve_real_cwd(project_dir: Path) -> Path | None:
    """Read the first jsonl event with a `cwd` field. Return None if no jsonl yields one.

    Iterates files newest first so a corrupted ancient session does not block resolution.
    """
    jsonl_files = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for jsonl in jsonl_files:
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for _ in range(CWD_SCAN_LINES):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = event.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return Path(cwd)
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class WorktreeGroup:
    """A set of projects that share a ``git_common_dir`` (worktrees of one repo)."""

    repo_root: Path
    members: tuple[Project, ...]

    @property
    def last_activity(self) -> float:
        return max(p.last_activity for p in self.members)

    @property
    def session_count(self) -> int:
        return sum(p.session_count for p in self.members)


@dataclass(frozen=True)
class ProjectFolder:
    """A user-defined folder containing projects (and possibly subfolders).

    ``name`` is the **full path** of the folder (e.g. ``"Trabajo/Cliente A"``).
    ``members`` are projects assigned **directly** to this folder. Subfolders
    are resolved on demand by FolderScreen via the store.

    ``descendant_member_count`` and ``descendant_session_count`` aggregate
    everything below (used by the ProjectsScreen row to summarise the subtree).
    """

    name: str
    members: tuple[Project, ...]
    descendant_member_count: int = 0
    descendant_session_count: int = 0
    descendant_last_activity: float = 0.0

    @property
    def last_activity(self) -> float:
        direct = max((p.last_activity for p in self.members), default=0.0)
        return max(direct, self.descendant_last_activity)

    @property
    def session_count(self) -> int:
        return sum(p.session_count for p in self.members) + self.descendant_session_count

    @property
    def total_member_count(self) -> int:
        return len(self.members) + self.descendant_member_count


def group_worktrees(projects: list[Project]) -> list[Project | WorktreeGroup]:
    """Collapse projects sharing ``git_common_dir`` into a :class:`WorktreeGroup`.

    Single-worktree repos and orphans pass through unchanged. The returned list
    preserves the relative ordering of the first occurrence of each group / loner.
    """
    bucket: OrderedDict[Path, list[Project]] = OrderedDict()
    loners: list[tuple[int, Project]] = []
    for idx, project in enumerate(projects):
        if project.git_common_dir is None:
            loners.append((idx, project))
            continue
        bucket.setdefault(project.git_common_dir, []).append(project)

    result: list[Project | WorktreeGroup] = []
    used_indices: dict[int, Project | WorktreeGroup] = {}
    for repo_root, members in bucket.items():
        if len(members) == 1:
            single = members[0]
            used_indices[projects.index(single)] = single
        else:
            group = WorktreeGroup(repo_root=repo_root, members=tuple(members))
            first_idx = min(projects.index(m) for m in members)
            used_indices[first_idx] = group
    for idx, project in loners:
        used_indices[idx] = project

    for idx in sorted(used_indices):
        result.append(used_indices[idx])
    return result


def resolve_git_common_dir(path: Path) -> Path | None:
    """Return ``git rev-parse --git-common-dir`` resolved to an absolute path, or None.

    Used to group multiple worktrees of the same repo under one entry. ``None`` means
    ``path`` is not inside a git repo or the binary is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (path / candidate).resolve()
    return candidate


def group_into_folders(
    rows: list[Project | WorktreeGroup],
    folder_of: dict[str, str],
) -> list[Project | WorktreeGroup | ProjectFolder]:
    """Pull folder-assigned projects out of ``rows`` and bundle them by folder name.

    ``folder_of`` maps ``str(encoded_path) → folder_name``. The grouping rule:

    - A ``WorktreeGroup`` whose every member shares the same folder is moved as
      a whole (the worktree-group is preserved inside the folder result via the
      individual member projects — folders contain only ``Project`` rows for
      simplicity; if you need the worktree structure, turn off the folder view).
    - A ``WorktreeGroup`` whose members are mixed (some assigned, some not) is
      split: assigned members go into their folders, the rest stays as a
      smaller ``WorktreeGroup`` (or a lone ``Project`` if only one survives).
    - Unassigned ``Project`` rows pass through untouched.

    Output preserves the relative order of the first occurrence of each
    folder / un-folded row.
    """
    root_buckets: dict[str, dict[str, list[Project]]] = {}
    root_first_idx: dict[str, int] = {}
    out: list[tuple[int, Project | WorktreeGroup | ProjectFolder]] = []

    def _route_project(project: Project, idx: int) -> None:
        folder = folder_of.get(str(project.encoded_path))
        if folder is None:
            out.append((idx, project))
            return
        root = folder.split("/", 1)[0]
        bucket = root_buckets.setdefault(root, {"direct": [], "descendants": []})
        if folder.casefold() == root.casefold():
            bucket["direct"].append(project)
        else:
            bucket["descendants"].append(project)
        if root not in root_first_idx:
            root_first_idx[root] = idx

    for idx, row in enumerate(rows):
        if isinstance(row, ProjectFolder):
            out.append((idx, row))
            continue
        if isinstance(row, WorktreeGroup):
            assignments = [folder_of.get(str(m.encoded_path)) for m in row.members]
            if all(a is None for a in assignments):
                out.append((idx, row))
                continue
            for member in row.members:
                _route_project(member, idx)
            unassigned = [m for m in row.members if folder_of.get(str(m.encoded_path)) is None]
            if len(unassigned) >= 2:
                out = [(i, r) for (i, r) in out if not (isinstance(r, Project) and r in unassigned)]
                out.append((idx, WorktreeGroup(repo_root=row.repo_root, members=tuple(unassigned))))
            continue
        _route_project(row, idx)

    for root, bucket in root_buckets.items():
        direct = bucket["direct"]
        descendants = bucket["descendants"]
        descendant_session_count = sum(p.session_count for p in descendants)
        descendant_last_activity = max((p.last_activity for p in descendants), default=0.0)
        out.append(
            (
                root_first_idx[root],
                ProjectFolder(
                    name=root,
                    members=tuple(direct),
                    descendant_member_count=len(descendants),
                    descendant_session_count=descendant_session_count,
                    descendant_last_activity=descendant_last_activity,
                ),
            )
        )

    out.sort(key=lambda pair: pair[0])
    return [row for _, row in out]
