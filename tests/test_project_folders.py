"""Tests for ProjectFoldersStore + discovery.group_into_folders."""

from __future__ import annotations

from pathlib import Path

import pytest

from multi_claude.discovery import (
    Project,
    ProjectFolder,
    WorktreeGroup,
    group_into_folders,
)
from multi_claude.project_folders import ProjectFoldersStore


def _project(encoded: Path, *, name: str = "p", common: Path | None = None) -> Project:
    return Project(
        name=name,
        path=Path("/real") / name,
        encoded_path=encoded,
        session_count=1,
        last_activity=0.0,
        is_orphan=False,
        git_common_dir=common,
    )


# -- store ----------------------------------------------------------------- #


def test_add_and_list(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    store.add_folder("Trabajo")
    store.add_folder("Personal")
    assert store.list_folders() == ["Trabajo", "Personal"]


def test_add_folder_is_idempotent_case_insensitive(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    store.add_folder("Trabajo")
    again = store.add_folder("trabajo")
    assert again == "Trabajo"
    assert store.list_folders() == ["Trabajo"]


def test_add_folder_rejects_empty(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    with pytest.raises(ValueError):
        store.add_folder("   ")


def test_assign_creates_folder_if_missing(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    encoded = tmp_path / "p1"
    store.assign(encoded, "Nueva")
    assert "Nueva" in store.list_folders()
    assert store.folder_of(encoded) == "Nueva"


def test_unassign(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    encoded = tmp_path / "p1"
    store.assign(encoded, "X")
    store.unassign(encoded)
    assert store.folder_of(encoded) is None
    assert "X" in store.list_folders()  # folder survives, just the assignment is gone


def test_rename_folder_moves_assignments(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    encoded = tmp_path / "p1"
    store.assign(encoded, "Old")
    store.rename_folder("Old", "Brand New")
    assert store.list_folders() == ["Brand New"]
    assert store.folder_of(encoded) == "Brand New"


def test_rename_folder_collision_is_rejected(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    store.add_folder("A")
    store.add_folder("B")
    with pytest.raises(ValueError):
        store.rename_folder("A", "b")  # case-insensitive collision


def test_delete_folder_unassigns_members(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    a, b = tmp_path / "a", tmp_path / "b"
    store.assign(a, "X")
    store.assign(b, "X")
    store.delete_folder("X")
    assert store.folder_of(a) is None
    assert store.folder_of(b) is None
    assert "X" not in store.list_folders()


def test_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    store_a = ProjectFoldersStore(path)
    store_a.assign(tmp_path / "p", "Persisted")

    store_b = ProjectFoldersStore(path)
    assert store_b.folder_of(tmp_path / "p") == "Persisted"
    assert "Persisted" in store_b.list_folders()


def test_dangling_assignments_are_dropped_on_load(tmp_path: Path) -> None:
    """If the file references a folder that's no longer in `folders`, drop the row."""
    path = tmp_path / "f.json"
    # Use platform-native path strings as keys, because ``folder_of`` does
    # ``str(Path(...))`` lookups and on Windows ``str(Path("/y"))`` is ``\\y``.
    key_y = str(Path("/y"))
    import json as _json

    path.write_text(
        _json.dumps({"folders": ["A"], "assignments": {"/x": "Ghost", key_y: "A"}}),
        encoding="utf-8",
    )
    store = ProjectFoldersStore(path)
    assert store.folder_of(tmp_path / "x") is None
    assert store.folder_of(Path("/y")) == "A"


def test_corrupt_file_is_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    store = ProjectFoldersStore(path)
    assert store.list_folders() == []
    assert store.all_assignments() == {}


# -- group_into_folders --------------------------------------------------- #


def test_group_into_folders_basic(tmp_path: Path) -> None:
    a = _project(tmp_path / "a", name="alpha")
    b = _project(tmp_path / "b", name="beta")
    c = _project(tmp_path / "c", name="gamma")
    folder_of = {str(a.encoded_path): "Work", str(b.encoded_path): "Work"}
    rows = group_into_folders([a, b, c], folder_of)
    assert len(rows) == 2
    folder = next(r for r in rows if isinstance(r, ProjectFolder))
    assert folder.name == "Work"
    assert {m.name for m in folder.members} == {"alpha", "beta"}
    # `c` stays as a plain project
    assert any(isinstance(r, Project) and r.name == "gamma" for r in rows)


def test_group_into_folders_preserves_unassigned(tmp_path: Path) -> None:
    a = _project(tmp_path / "a", name="alpha")
    rows = group_into_folders([a], folder_of={})
    assert rows == [a]


def test_group_into_folders_splits_mixed_worktree_group(tmp_path: Path) -> None:
    common = tmp_path / "repo/.git"
    w1 = _project(tmp_path / "w1", name="main", common=common)
    w2 = _project(tmp_path / "w2", name="feat", common=common)
    w3 = _project(tmp_path / "w3", name="hotfix", common=common)
    group = WorktreeGroup(repo_root=common, members=(w1, w2, w3))
    folder_of = {str(w1.encoded_path): "Work"}
    rows = group_into_folders([group], folder_of)

    # 'Work' folder with w1 + a smaller WorktreeGroup with w2,w3
    folder = next(r for r in rows if isinstance(r, ProjectFolder))
    assert {m.name for m in folder.members} == {"main"}
    remnant = next(r for r in rows if isinstance(r, WorktreeGroup))
    assert {m.name for m in remnant.members} == {"feat", "hotfix"}


def test_group_into_folders_passes_whole_worktree_group_when_all_assigned(
    tmp_path: Path,
) -> None:
    """Every member of a worktree-group sharing the same folder → all three end up in the folder."""
    common = tmp_path / "repo/.git"
    w1 = _project(tmp_path / "w1", name="main", common=common)
    w2 = _project(tmp_path / "w2", name="feat", common=common)
    group = WorktreeGroup(repo_root=common, members=(w1, w2))
    folder_of = {str(w1.encoded_path): "Acme", str(w2.encoded_path): "Acme"}
    rows = group_into_folders([group], folder_of)
    assert len(rows) == 1
    assert isinstance(rows[0], ProjectFolder)
    assert {m.name for m in rows[0].members} == {"main", "feat"}


# -- nested folder behaviour -------------------------------------------- #


def test_nested_add_creates_ancestors(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    store.add_folder("Work/Cliente A/Backend")
    assert set(store.list_folders()) == {"Work", "Work/Cliente A", "Work/Cliente A/Backend"}


def test_nested_collapses_double_slashes(tmp_path: Path) -> None:
    """``Work//Cliente A`` is forgiving — the empty segment is dropped."""
    store = ProjectFoldersStore(tmp_path / "f.json")
    canonical = store.add_folder("Work//Cliente A")
    assert canonical == "Work/Cliente A"
    assert "Work" in store.list_folders()
    assert "Work/Cliente A" in store.list_folders()


def test_nested_validation_rejects_only_slashes(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    with pytest.raises(ValueError):
        store.add_folder("//")


def test_nested_rename_propagates_to_descendants(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    encoded = tmp_path / "p"
    store.assign(encoded, "Work/Cliente A/Backend")
    store.rename_folder("Work/Cliente A", "Cliente B")
    assert set(store.list_folders()) == {"Work", "Work/Cliente B", "Work/Cliente B/Backend"}
    assert store.folder_of(encoded) == "Work/Cliente B/Backend"


def test_nested_delete_cascades(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    e1, e2 = tmp_path / "p1", tmp_path / "p2"
    store.assign(e1, "Work/Cliente A")
    store.assign(e2, "Work/Cliente A/Backend")
    store.delete_folder("Work/Cliente A")
    assert "Work" in store.list_folders()  # ancestor survives
    assert "Work/Cliente A" not in store.list_folders()
    assert "Work/Cliente A/Backend" not in store.list_folders()
    assert store.folder_of(e1) is None
    assert store.folder_of(e2) is None


def test_children_folders_root(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    store.add_folder("A")
    store.add_folder("B/Child")
    assert set(store.children_folders(None)) == {"A", "B"}


def test_children_folders_of_nested(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    store.add_folder("Work/Cliente A")
    store.add_folder("Work/Cliente B")
    store.add_folder("Work/Cliente A/Backend")
    assert set(store.children_folders("Work")) == {"Work/Cliente A", "Work/Cliente B"}
    assert set(store.children_folders("Work/Cliente A")) == {"Work/Cliente A/Backend"}


def test_members_of_recursive(tmp_path: Path) -> None:
    store = ProjectFoldersStore(tmp_path / "f.json")
    a, b = tmp_path / "a", tmp_path / "b"
    store.assign(a, "Work")
    store.assign(b, "Work/Cliente A")
    assert set(store.members_of("Work", recursive=False)) == {str(a)}
    assert set(store.members_of("Work", recursive=True)) == {str(a), str(b)}


def test_group_into_folders_collapses_to_root(tmp_path: Path) -> None:
    """Nested assignments roll up to a single root-level ProjectFolder."""
    p1 = _project(tmp_path / "p1", name="alpha")
    p2 = _project(tmp_path / "p2", name="beta")
    p3 = _project(tmp_path / "p3", name="gamma")
    folder_of = {
        str(p1.encoded_path): "Work",
        str(p2.encoded_path): "Work/Cliente A",
        str(p3.encoded_path): "Work/Cliente A/Backend",
    }
    rows = group_into_folders([p1, p2, p3], folder_of)
    assert len(rows) == 1
    folder = rows[0]
    assert isinstance(folder, ProjectFolder)
    assert folder.name == "Work"
    # Direct members = only those at the root level
    assert {m.name for m in folder.members} == {"alpha"}
    assert folder.descendant_member_count == 2
    assert folder.total_member_count == 3
