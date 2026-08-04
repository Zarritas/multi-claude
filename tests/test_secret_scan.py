"""Tests for the pre-publish credential scan.

Three things are under test, in order of how badly getting them wrong would hurt:

1. **Nothing is echoed verbatim.** A scanner that prints what it found leaks it again.
2. **Real credentials are caught**, at least the shapes that have a recognisable prefix.
3. **Ordinary conversation does not trip it.** A scanner that cries wolf gets ignored, and
   an ignored scanner is worse than none because it buys false confidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from multi_claude.secret_scan import (
    GENERIC_ROTATION,
    MAX_FILE_BYTES,
    MAX_FINDINGS,
    ROTATION,
    RULES,
    Finding,
    group_findings,
    mask,
    redact,
    scan_files,
    scan_text,
    skipped_files,
)

HERE = Path("/p/session.jsonl")


def rules_of(text: str) -> list[str]:
    return [f.rule for f in scan_text(text, HERE)]


# Every credential-shaped fixture below is assembled at runtime rather than written out.
# Not style: GitHub's push protection scans this file too, and a literal `sk_live_…`
# blocks the push of the very test that proves we detect it. A scanner cannot tell a
# fixture from the real thing — which is the whole premise of the module under test.
def _like(prefix: str, body: str) -> str:
    return prefix + body


# --- the secret never comes back out -------------------------------------------------

REAL_LOOKING = _like("ghp", "_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456")


def test_the_finding_does_not_contain_the_secret() -> None:
    (finding,) = scan_text(f"export GITHUB_TOKEN={REAL_LOOKING}", HERE)
    assert REAL_LOOKING not in finding.excerpt
    assert REAL_LOOKING not in finding.describe()
    assert REAL_LOOKING not in repr(finding)


def test_the_mask_keeps_enough_to_recognise_but_not_to_use() -> None:
    masked = mask(REAL_LOOKING)
    assert masked.startswith("ghp_")
    assert REAL_LOOKING[4:-2] not in masked
    assert str(len(REAL_LOOKING)) in masked  # the length is a useful hint


@pytest.mark.parametrize("value", ["", "a", "abc", "12345678"])
def test_mask_survives_short_values(value: str) -> None:
    masked = mask(value)
    assert value[1:] not in masked or len(value) <= 1


def test_describe_shortens_the_path_against_a_root() -> None:
    finding = Finding(path=Path("/p/sub/tool-results/x.txt"), line=3, rule="r", excerpt="e")
    assert finding.describe(Path("/p")) == "sub/tool-results/x.txt:3 · r · e"


def test_describe_keeps_a_path_outside_the_root() -> None:
    finding = Finding(path=Path("/elsewhere/x.txt"), line=1, rule="r", excerpt="e")
    assert finding.describe(Path("/p")).startswith("/elsewhere/x.txt")


# --- redacting text that gets echoed back --------------------------------------------


def test_redact_replaces_the_secret_with_its_mask() -> None:
    out = redact(f"export GITHUB_TOKEN={REAL_LOOKING} && deploy")
    assert REAL_LOOKING not in out
    assert out.startswith("export GITHUB_TOKEN=ghp_")
    assert out.endswith("&& deploy")


def test_redact_accepts_a_fixed_marker() -> None:
    assert redact(f"TOKEN={REAL_LOOKING}", "[oculto]") == "TOKEN=[oculto]"


def test_redact_leaves_ordinary_text_alone() -> None:
    text = "hablemos del token que hay que rotar la semana que viene"
    assert redact(text) == text


def test_redact_handles_several_secrets_in_one_string() -> None:
    out = redact(f"a={REAL_LOOKING} b=" + _like("AKIA", "IOSFODNN7EXAMPLE"), "[x]")
    assert out == "a=[x] b=[x]"


def test_redact_is_stable_when_one_secret_contains_another() -> None:
    """Longest first, so a shorter match inside a longer one cannot corrupt the result."""
    out = redact(f"Authorization: Bearer {REAL_LOOKING}", "[x]")
    assert REAL_LOOKING not in out


# --- what it must catch ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "clave privada"),
        ("-----BEGIN RSA PRIVATE KEY-----", "clave privada"),
        ("-----BEGIN PRIVATE KEY-----", "clave privada"),
        ("AWS_ACCESS_KEY_ID=" + _like("AKIA", "IOSFODNN7EXAMPLE"), "AWS access key"),
        (
            'aws_secret_access_key = "'
            + _like("wJalrXUtnFEMI/K7MDENG/", "bPxRfiCYEXAMPLEKEY")
            + '"',
            "AWS secret key",
        ),
        (f"token: {REAL_LOOKING}", "token de GitHub"),
        (_like("glpat", "-xxxxxxxxxxxxxxxxxxxx1"), "token de GitLab"),
        (
            "ANTHROPIC_API_KEY=" + _like("sk-ant", "-api03-abcdefghijklmnopqrstuvwx"),
            "clave de API de Anthropic",
        ),
        (_like("xoxb", "-123456789012-abcdefghijkl"), "token de Slack"),
        # AIza + exactly 35 chars, which is the shape Google issues.
        (_like("AIza", "SyD-1234567890abcdefghijklmnopqrstu"), "clave de API de Google"),
        (_like("sk", "_live_abcdefghijklmnopqrstuvwx"), "token de Stripe"),
        (
            # Three dot-separated sections of 10+ chars each, which is what the rule wants.
            _like("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", ".eyJzdWIiOiIxMjM0NTY3ODkwIn0")
            + ".dBjftJeZ4CVP-mB92K",
            "JWT",
        ),
        ("postgres://usuario:hunter2secreto@db.interno:5432/prod", "credenciales en una URL"),
        ("Authorization: Bearer abcdef1234567890abcdef", "cabecera Authorization"),
        ('DB_PASSWORD="Tr0ub4dor&3xyz"', "asignación con nombre de secreto"),
        ("api_key: 9f8e7d6c5b4a3210", "asignación con nombre de secreto"),
    ],
)
def test_catches_a_credential(text: str, rule: str) -> None:
    assert rules_of(text) == [rule], text


def test_catches_every_secret_on_one_jsonl_line() -> None:
    """The realistic case: a Bash call that printed a .env, recorded in the transcript.

    A jsonl escapes its newlines, so the whole tool result is one physical line — both
    credentials have to be reported, in the order they appear.
    """
    line = (
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
        '"content":"DATABASE_URL=postgres://app:sup3rs3cr3t@db:5432/prod\\n'
        f'GITHUB_TOKEN={REAL_LOOKING}"}}]}}}}'
    )
    assert rules_of(line) == ["credenciales en una URL", "token de GitHub"]


def test_the_same_value_is_reported_once_per_line() -> None:
    """A token that trips both the generic rule and its own is one secret, not two."""
    assert rules_of(f"GITHUB_TOKEN={REAL_LOOKING}") == ["token de GitHub"]


def test_reports_the_line_number() -> None:
    text = "línea uno\nlínea dos\nDB_PASSWORD=Tr0ub4dor3xyz\n"
    (finding,) = scan_text(text, HERE)
    assert finding.line == 3


def test_start_line_offsets_the_report() -> None:
    (finding,) = scan_text("DB_PASSWORD=Tr0ub4dor3xyz", HERE, start_line=100)
    assert finding.line == 100


# --- what it must NOT flag -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hablemos del password que hay que cambiar",
        "el token expiró y hay que renovarlo",
        "PASSWORD=changeme",
        "API_KEY=your-api-key-here",
        "SECRET=xxxxxxxx",
        "token = TODO",
        'password: "<pon aquí la tuya>"',
        "API_TOKEN=${GITHUB_TOKEN}",
        "api_key: {{ vault_api_key }}",
        "SECRET_KEY=%SECRET_KEY%",
        "TIMEOUT_TOKEN=30",
        "password: null",
        "api_key: undefined",
        "AWS_SECRET_ACCESS_KEY=example",
        "def rotate_token(self, token: str) -> None:",
        "assert response.json()['token'] is not None",
        "# el api_key va en el header, no en la query",
        "grep -r PASSWORD .",
        "psql postgres://usuario@localhost:5432/dev",  # no password in the URL
    ],
)
def test_does_not_flag_ordinary_text(text: str) -> None:
    assert rules_of(text) == [], text


def test_a_long_base64_blob_is_not_a_credential_by_itself() -> None:
    """No secret-ish name, no known prefix: flagging this would flag every attachment."""
    assert rules_of("data = " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 4) == []


def test_placeholder_check_ignores_surrounding_quotes() -> None:
    assert rules_of("password = 'changeme'") == []


# --- files -----------------------------------------------------------------------------


def test_scan_files_walks_every_path(tmp_path: Path) -> None:
    clean = tmp_path / "clean.txt"
    clean.write_text("nada raro por aquí\n")
    dirty = tmp_path / "dirty.txt"
    dirty.write_text(f"GITHUB_TOKEN={REAL_LOOKING}\n")
    findings = scan_files([clean, dirty])
    assert [f.path for f in findings] == [dirty]


def test_scan_files_ignores_a_missing_path(tmp_path: Path) -> None:
    """Scanning must never be what breaks a publish."""
    assert scan_files([tmp_path / "no-existe.jsonl"]) == []


def test_scan_files_skips_binary(tmp_path: Path) -> None:
    blob = tmp_path / "captura.png"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + (_like("ghp", "_" + "a" * 36)).encode())
    assert scan_files([blob]) == []
    assert skipped_files([blob]) == [blob]


def test_scan_files_skips_a_file_over_the_size_cap(tmp_path: Path) -> None:
    huge = tmp_path / "huge.jsonl"
    huge.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    assert scan_files([huge]) == []
    assert skipped_files([huge]) == [huge]


def test_skipped_files_is_empty_for_readable_text(tmp_path: Path) -> None:
    ok = tmp_path / "ok.jsonl"
    ok.write_text("hola\n")
    assert skipped_files([ok]) == []


def test_undecodable_bytes_are_still_scanned(tmp_path: Path) -> None:
    """Latin-1 in a transcript must not become a blind spot."""
    path = tmp_path / "mixed.txt"
    path.write_bytes("contraseña\n".encode("latin-1") + f"GITHUB_TOKEN={REAL_LOOKING}\n".encode())
    assert [f.rule for f in scan_files([path])] == ["token de GitHub"]


def test_findings_are_capped(tmp_path: Path) -> None:
    path = tmp_path / "many.txt"
    path.write_text("\n".join(f"DB_PASSWORD=Tr0ub4dor{i}xyz" for i in range(MAX_FINDINGS * 3)))
    assert len(scan_files([path])) == MAX_FINDINGS


def test_the_same_secret_repeated_is_one_finding_with_a_count(tmp_path: Path) -> None:
    """A key printed by a command that ran 70 times is one secret, not 70 rows."""
    path = tmp_path / "repeated.txt"
    path.write_text(f"APIKEY={'c21f' * 10}\n" * 70)
    (finding,) = scan_files([path])
    assert finding.occurrences == 70
    assert finding.line == 1  # where it was first seen
    assert "70 veces" in finding.describe()


def test_two_different_secrets_are_not_merged_by_a_shared_prefix(tmp_path: Path) -> None:
    """Dedup is on the value, not on its mask; same prefix and length must stay separate."""
    path = tmp_path / "two.txt"
    path.write_text("APIKEY=c21fAAAAAAAAAAAAAAAAAAAA\nAPIKEY=c21fBBBBBBBBBBBBBBBBBBBB\n")
    findings = scan_files([path])
    assert len(findings) == 2
    assert all(f.occurrences == 1 for f in findings)


def test_a_single_finding_says_nothing_about_repeats(tmp_path: Path) -> None:
    path = tmp_path / "one.txt"
    path.write_text(f"GITHUB_TOKEN={REAL_LOOKING}\n")
    (finding,) = scan_files([path])
    assert "veces" not in finding.describe()


def test_a_very_long_line_does_not_hang_the_scan(tmp_path: Path) -> None:
    """A jsonl can hold a whole file inlined in one line; truncation keeps it cheap."""
    path = tmp_path / "long.jsonl"
    path.write_text("x" * 5_000_000 + f" GITHUB_TOKEN={REAL_LOOKING}\n")
    assert scan_files([path]) == []  # past the truncation point, so not seen — and fast


# --- grouping into something actionable -------------------------------------------------


def test_findings_of_one_rule_become_one_row_that_says_what_to_rotate() -> None:
    """The reason grouping exists: the reader needs a key to rotate, not a line number.

    Seven matches of the same rule are one instruction, and the instruction has to name
    the issuer — that is what a reader can act on without opening the transcript.
    """
    text = "\n".join(f"GITHUB_TOKEN={REAL_LOOKING}" for _ in range(7))
    (exposure,) = group_findings(scan_text(text, HERE))
    assert exposure.rule == "token de GitHub"
    assert exposure.distinct == 1
    assert exposure.occurrences == 7
    assert "GitHub" in exposure.rotation
    assert "7 apariciones" in exposure.headline()


def test_every_rule_with_a_known_issuer_has_rotation_advice() -> None:
    """A typo in a ROTATION key would silently downgrade a real issuer to the vague hint.

    The generic rule is the one exception, by definition: it matches on a variable's name
    and so cannot know who issued the value.
    """
    missing = [rule.name for rule in RULES if not rule.strict_value and rule.name not in ROTATION]
    assert missing == []
    assert set(ROTATION) <= {rule.name for rule in RULES}


def test_the_generic_rule_admits_it_does_not_know_the_issuer() -> None:
    (exposure,) = group_findings(scan_text("DB_PASSWORD=Tr0ub4dor3xyz!", HERE))
    assert exposure.rotation == GENERIC_ROTATION


def test_two_different_values_of_one_rule_keep_both_masks() -> None:
    """Grouping must not claim one key where there are two: both have to be rotated."""
    other = _like("ghp", "_zYxWvUtSrQpOnMlKjIhGfEdCbA9876543")
    (exposure,) = group_findings(scan_text(f"a={REAL_LOOKING}\nb={other}", HERE))
    assert exposure.distinct == 2
    assert "2 distintas" in exposure.headline()
    assert len(exposure.excerpts) == 2


def test_the_grouped_row_never_carries_the_secret() -> None:
    (exposure,) = group_findings(scan_text(f"GITHUB_TOKEN={REAL_LOOKING}", HERE))
    rendered = f"{exposure.headline()} {exposure.rotation} {exposure.where()} {exposure!r}"
    assert REAL_LOOKING not in rendered


def test_rules_with_a_known_issuer_are_listed_before_the_guesses() -> None:
    """A confirmed GitHub token outranks an assignment that merely looks suspicious."""
    text = f"DB_PASSWORD=Tr0ub4dor3xyz!\nGITHUB_TOKEN={REAL_LOOKING}"
    assert [e.rule for e in group_findings(scan_text(text, HERE))] == [
        "token de GitHub",
        "asignación con nombre de secreto",
    ]


def test_locations_are_relative_to_the_project_and_capped(tmp_path: Path) -> None:
    """Four different tokens on four lines: the row names the first places, then counts."""
    lines = "\n".join("GITHUB_TOKEN=" + _like("ghp", f"_{letter * 34}") for letter in "abcd")
    (exposure,) = group_findings(scan_text(lines, tmp_path / "sub" / "ses.jsonl"), tmp_path)
    where = exposure.where()
    assert where.startswith("sub/ses.jsonl:1")
    assert str(tmp_path) not in where  # relative, so the row fits on a line
    assert "y 2 sitio(s) más" in where


def test_one_rule_matching_twice_on_a_line_is_one_place() -> None:
    """Same line, two values: two credentials to rotate but only one place to look."""
    other = _like("ghp", "_zYxWvUtSrQpOnMlKjIhGfEdCbA9876543")
    (exposure,) = group_findings(scan_text(f"a={REAL_LOOKING} b={other}", HERE))
    assert exposure.distinct == 2
    assert len(exposure.locations) == 1


def test_nothing_found_groups_to_nothing() -> None:
    assert group_findings([]) == []
