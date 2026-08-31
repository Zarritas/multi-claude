"""What a release depends on: the `--version` flag, and the notes the workflow publishes.

`tools/release_notes.py` is exercised as a subprocess rather than imported, because that is
how the workflow calls it: what has to hold is the exit code and what lands on stdout, not
the signature of a function nothing else imports.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from multi_claude.__main__ import main

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "release_notes.py"

CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

- Nothing yet.

## [1.0.0] - 2026-08-31

### Added

- The thing that matters.

### Fixed

- The other thing.

## [0.1.0] - 2026-05-22

Initial MVP release.

[Unreleased]: https://example.invalid/compare/v1.0.0...HEAD
[1.0.0]: https://example.invalid/releases/tag/v1.0.0
"""


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def changelog(tmp_path: Path) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    return path


# --- the --version flag ----------------------------------------------------------------


def test_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    """Anyone reporting a bug needs it, and argparse exits 0 after printing."""
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("multi-claude ")


def test_version_flag_does_not_open_the_tui(capsys: pytest.CaptureFixture[str]) -> None:
    """It exits during parsing, so nothing after it in main() ever runs."""
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "multi-claude" in capsys.readouterr().out


# --- the release notes -----------------------------------------------------------------


def test_extracts_the_body_of_one_version(changelog: Path) -> None:
    result = run("1.0.0", "--changelog", str(changelog))
    assert result.returncode == 0
    assert "The thing that matters." in result.stdout
    assert "### Fixed" in result.stdout


def test_stops_before_the_previous_version(changelog: Path) -> None:
    """Otherwise every release would carry the whole history as its notes."""
    result = run("1.0.0", "--changelog", str(changelog))
    assert "Initial MVP release." not in result.stdout


def test_does_not_reach_back_into_unreleased(changelog: Path) -> None:
    result = run("1.0.0", "--changelog", str(changelog))
    assert "Nothing yet." not in result.stdout


def test_drops_the_link_definitions(changelog: Path) -> None:
    """They close the document, not the version, and read as noise in a release body."""
    result = run("1.0.0", "--changelog", str(changelog))
    assert "example.invalid" not in result.stdout


def test_the_heading_itself_is_not_repeated(changelog: Path) -> None:
    """GitHub already shows the version as the release's title."""
    result = run("1.0.0", "--changelog", str(changelog))
    assert "## [1.0.0]" not in result.stdout


def test_accepts_the_tag_form(changelog: Path) -> None:
    """The workflow has a tag ref in hand, not a bare version."""
    tagged = run("v1.0.0", "--changelog", str(changelog))
    bare = run("1.0.0", "--changelog", str(changelog))
    assert tagged.returncode == 0
    assert tagged.stdout == bare.stdout


def test_a_missing_section_fails_the_release(changelog: Path) -> None:
    """Tagging without closing [Unreleased] must stop the workflow, not publish empty notes."""
    result = run("2.0.0", "--changelog", str(changelog))
    assert result.returncode == 1
    assert "no tiene una sección para 2.0.0" in result.stderr


def test_the_error_says_how_to_fix_it(changelog: Path) -> None:
    result = run("2.0.0", "--changelog", str(changelog))
    assert "[Unreleased]" in result.stderr
    assert "## [2.0.0]" in result.stderr


def test_an_unreadable_changelog_is_not_a_missing_section(tmp_path: Path) -> None:
    """Exit 2, so 'the file is not there' never reads as 'you forgot the notes'."""
    result = run("1.0.0", "--changelog", str(tmp_path / "nope.md"))
    assert result.returncode == 2


def test_an_empty_section_is_refused(tmp_path: Path) -> None:
    """A heading with nothing under it is the same mistake as no heading at all."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## [1.0.0] - 2026-08-31\n\n## [0.1.0] - 2026-05-22\n\nOld.\n")
    result = run("1.0.0", "--changelog", str(path))
    assert result.returncode == 1


# --- against the real file -------------------------------------------------------------


def test_the_repo_changelog_has_notes_for_its_latest_version() -> None:
    """Guards the released file itself: the top version must have a body to publish."""
    lines = (REPO / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    versions = [
        line.split("[", 1)[1].split("]", 1)[0]
        for line in lines
        if line.startswith("## [") and "Unreleased" not in line
    ]
    assert versions, "the CHANGELOG has no released version yet"
    result = run(versions[0])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
