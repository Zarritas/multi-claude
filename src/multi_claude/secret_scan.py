"""Look for credentials in the files a session is about to publish.

A transcript drags along everything the conversation touched: a ``Bash`` call that printed
a ``.env``, a ``cat`` of ``id_rsa``, a token pasted into a prompt. Publishing that to a
repository the whole team reads is the failure mode most likely to get this feature banned
in an organisation, so the publish dialogue asks here first.

Two rules shape the design:

**Nothing found is ever printed verbatim.** A scanner that echoes the secret it found into
a dialogue — and from there into a screenshot, a scrollback or a bug report — has leaked it
a second time. :class:`Finding` carries a *masked* excerpt and the rule's name; the value
never leaves this module.

**A finding is a warning, not a veto.** Heuristics over arbitrary conversation text produce
false positives (a base64 blob, an example key in documentation), and a scanner that
refuses to publish teaches people to work around it. The dialogue makes the risk loud and
the accept path deliberate; the decision stays with the person.

What the dialogue shows is not the raw findings but :class:`Exposure` — one row per
*issuer*, carrying what to do about it. A :class:`Finding` answers "where did this match",
which is the question a reader has already answered by the time they are looking at the
dialogue; the one still open is "what do I have to rotate". Seven rows reading
``token de GitHub`` at seven line numbers hide that answer instead of giving it.

Named ``secret_scan`` rather than ``secrets`` so it cannot be confused with the stdlib
module of that name.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, replace
from pathlib import Path

# Lines longer than this are truncated before matching: a jsonl can hold a single line with
# a whole file base64-encoded inside, and regexes over megabytes of one line are where a
# scanner stops being cheap.
MAX_LINE_CHARS = 4_000

# Per file, so scanning a publish of several multi-megabyte transcripts stays interactive.
MAX_FILE_BYTES = 8 * 1024 * 1024

# How many findings are collected before giving up: past a handful the answer is already
# "review this by hand", and the dialogue cannot show hundreds anyway.
MAX_FINDINGS = 50


@dataclass(frozen=True)
class Finding:
    """One suspected credential. ``excerpt`` is masked and safe to display.

    ``line`` is where it was first seen and ``occurrences`` how many times that same value
    appears in the file: a key printed by a command that ran seventy times is one secret
    to deal with, and listing it seventy times would bury everything else.
    """

    path: Path
    line: int
    rule: str
    excerpt: str
    occurrences: int = 1

    def describe(self, root: Path | None = None) -> str:
        """One line for a report: where, which rule, and a masked sample."""
        repeats = f" ({self.occurrences} veces)" if self.occurrences > 1 else ""
        return f"{_shown_path(self.path, root)}:{self.line} · {self.rule} · {self.excerpt}{repeats}"


def _shown_path(path: Path, root: Path | None) -> Path:
    if root is None:
        return path
    with contextlib.suppress(ValueError):  # not under root: show the full path
        return path.relative_to(root)
    return path


# What to do about each issuer, so a warning becomes an instruction. Keyed on the rule's
# name, and deliberately without URLs or menu paths: a provider rearranges its console far
# more often than this file changes, and a stale click-path is worse than none.
#
# Every entry says *revoke or rotate*, never "remove it from the transcript". By the time
# this text is on screen the credential has been sitting in plain text on the disk since
# the conversation happened; not publishing it limits the blast radius but does not undo
# the exposure.
ROTATION: dict[str, str] = {
    "clave privada": "regenera el par y retira la pública de donde estuviera autorizada",
    "AWS access key": "desactiva la clave en IAM y crea otra",
    "AWS secret key": "desactiva la clave en IAM y crea otra",
    "token de GitHub": "revócalo en los tokens de acceso de GitHub",
    "token de GitLab": "revócalo en los tokens de acceso de GitLab",
    "clave de API de Anthropic": "revócala en la consola de Anthropic",
    "clave de API de OpenAI": "revócala en la consola de OpenAI",
    "token de Slack": "revócalo en la configuración de la app de Slack",
    "clave de API de Google": "revócala en las credenciales del proyecto de Google Cloud",
    "token de Stripe": "rótala en el panel de Stripe — es una clave «live»",
    "JWT": "caduca solo, pero mira qué lo firmó: si hay una clave detrás, rótala",
    "credenciales en una URL": "cambia la contraseña del servicio al que apunta",
    "cabecera Authorization": "depende de quién la emitiera: abre la línea para saberlo",
}

# For the generic rule, which matches on the variable's *name* and so cannot know who
# issued the value. Saying "look at it" is the honest advice, and it is also the rule most
# likely to be a false positive.
GENERIC_ROTATION = "sin emisor reconocible: abre la línea y decide qué es"

# How many masked samples one row shows before it just gives the count.
_EXCERPTS_SHOWN = 2

# How many places one row names before it says "and N more".
_LOCATIONS_SHOWN = 2


@dataclass(frozen=True)
class Exposure:
    """Every finding of one rule, as the single thing it probably is: a key to rotate.

    ``distinct`` counts different values, ``occurrences`` how many times they appear in
    total. ``excerpts`` are masked, like the findings they come from.
    """

    rule: str
    distinct: int
    occurrences: int
    excerpts: tuple[str, ...]
    locations: tuple[str, ...]

    @property
    def rotation(self) -> str:
        """What to do about it."""
        return ROTATION.get(self.rule, GENERIC_ROTATION)

    def headline(self, *, excerpts: bool = True) -> str:
        """The issuer, a masked sample or two, and how much of it there is.

        ``excerpts=False`` drops the masked samples, for output meant to be safe to paste
        somewhere — a ticket does not need even the first four characters of a key.
        """
        parts = [self.rule]
        if self.distinct > 1:
            parts.append(f"{self.distinct} distintas")
        if excerpts:
            parts.extend(self.excerpts[:_EXCERPTS_SHOWN])
            if self.distinct > _EXCERPTS_SHOWN:
                parts.append(f"y {self.distinct - _EXCERPTS_SHOWN} más")
        if self.occurrences > self.distinct:
            parts.append(f"{self.occurrences} apariciones")
        return " · ".join(parts)

    def where(self) -> str:
        """The first places it turns up, capped: the file list is not the point here."""
        shown = self.locations[:_LOCATIONS_SHOWN]
        hidden = len(self.locations) - len(shown)
        listed = ", ".join(shown)
        return f"{listed} y {hidden} sitio(s) más" if hidden > 0 else listed


def group_findings(findings: list[Finding], root: Path | None = None) -> list[Exposure]:
    """Collapse findings into one :class:`Exposure` per rule, keeping the rules' order.

    Two findings of the same rule are the same credential often enough — a token printed
    by one command that ran seventy times, a key that appears in both the transcript and a
    tool result — that the grouping is what makes the count readable. Where it is wrong
    (two different GitHub tokens in one session) ``distinct`` says so and both masks show.

    The order comes from :data:`_RULE_ORDER`, defined with the rules further down.
    """
    excerpts: dict[str, list[str]] = {}
    locations: dict[str, list[str]] = {}
    occurrences: dict[str, int] = {}
    for finding in findings:
        rule = finding.rule
        excerpts.setdefault(rule, []).append(finding.excerpt)
        occurrences[rule] = occurrences.get(rule, 0) + finding.occurrences
        where = f"{_shown_path(finding.path, root)}:{finding.line}"
        seen = locations.setdefault(rule, [])
        if where not in seen:  # a rule matching twice on one line is one place, not two
            seen.append(where)
    grouped = [
        Exposure(
            rule=rule,
            distinct=len(rule_excerpts),
            occurrences=occurrences[rule],
            excerpts=tuple(rule_excerpts),
            locations=tuple(locations[rule]),
        )
        for rule, rule_excerpts in excerpts.items()
    ]
    return sorted(grouped, key=lambda e: rule_order(e.rule))


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    # Which group holds the secret itself, so only that part gets masked.
    group: int = 0
    # Whether the captured value must also *look* like a credential. On for the rule that
    # matches by variable name, where the name alone is far too weak a signal; off for the
    # ones keyed on an issuer's own prefix, which is evidence enough by itself.
    strict_value: bool = False


def _rules() -> tuple[_Rule, ...]:
    """The patterns, ordered so the specific ones report before the generic ones."""
    return (
        _Rule("clave privada", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
        _Rule("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
        _Rule(
            "AWS secret key",
            re.compile(r"(?i)aws_?secret_?access_?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})\b"),
            1,
        ),
        _Rule("token de GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
        _Rule("token de GitLab", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
        _Rule("clave de API de Anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
        _Rule("clave de API de OpenAI", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
        _Rule("token de Slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
        _Rule("clave de API de Google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
        _Rule("token de Stripe", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
        _Rule(
            "JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
        ),
        _Rule(
            "credenciales en una URL",
            re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:([^\s/@]{3,})@"),
            1,
        ),
        _Rule(
            "cabecera Authorization",
            re.compile(
                r"(?i)authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+([A-Za-z0-9._~+/=\-]{12,})"
            ),
            1,
        ),
        # The generic one, last: an assignment whose *name* claims it is a credential.
        #
        # The keyword may carry a prefix (`github_token`) or an underscored suffix
        # (`token_value`), but NOT a bare letter suffix: that is what separates a real
        # `token=` from `tokenize(`, `tokens:` or `input_tokens:` — measured against real
        # transcripts, those three alone accounted for most of the noise.
        # The value is then held to :func:`_looks_like_credential`, because a name is a
        # weak signal: `has_token = True` and `max_tokens: 4096` are not secrets.
        _Rule(
            "asignación con nombre de secreto",
            re.compile(
                r"(?i)\b[A-Za-z0-9]*_?"
                r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
                r"private[_-]?key|credential)"
                r"(?:_[A-Za-z0-9]+)?"
                r"[\"']?\s*[:=]\s*[\"']?([^\s\"',;)]{8,})"
            ),
            1,
            strict_value=True,
        ),
    )


RULES = _rules()

# Rules report in the order they are declared — specific before generic — and
# :func:`group_findings` preserves it, so what has a known issuer (and therefore a real
# action) reads before the guesses.
_RULE_ORDER: dict[str, int] = {rule.name: index for index, rule in enumerate(RULES)}


def rule_order(rule: str) -> int:
    """Where ``rule`` sits among the rules, for callers that sort issuers themselves.

    The history sweep aggregates across sessions and cannot reuse :func:`group_findings`
    for the order, but it must present issuers the same way the dialogue does.
    """
    return _RULE_ORDER.get(rule, len(_RULE_ORDER))


# Values that look like a credential but are obviously not one. Checked against the
# generic rule only, where the false-positive rate would otherwise make the scanner noise.
_PLACEHOLDER_RE = re.compile(
    r"(?i)\A(?:"
    r"true|false|null|none|nil|undefined|empty|"
    r"x{3,}|\*{3,}|\.{3,}|-{3,}|_{3,}|"
    # [\w\-]* rather than \w*: `your-api-key-here` is the canonical placeholder and \w
    # stops at the first hyphen.
    r"your[\w\-]*|my[\w\-]*|some[\w\-]*|example[\w\-]*|sample[\w\-]*|dummy[\w\-]*|"
    r"placeholder[\w\-]*|redacted[\w\-]*|changeme[\w\-]*|todo[\w\-]*|fixme[\w\-]*|"
    r"test[\w\-]*|fake[\w\-]*|"
    r"secret|password|token|apikey|api[_\-]key|value|string|"
    r"\$\{?\w+\}?|\{\{?\w+\}?\}|<[^>]+>|%\w+%|"
    r"\d+"  # a bare number is a port, a timeout, an id
    r")\Z"
)


def mask(value: str) -> str:
    """A displayable stand-in: enough to recognise the string, not enough to use it."""
    if len(value) <= 8:
        return value[0] + "…" * min(3, len(value) - 1) if value else ""
    return f"{value[:4]}…{value[-2:]} ({len(value)} car.)"


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(value.strip("\"'`")))


# A path, a URL or an identifier assigned to something called `token` is code, not a
# credential. Case-sensitive on purpose: with a global (?i) the "pure lowercase
# identifier" branch also swallows `Tr0ub4dor3xyz`, which is exactly what must be caught.
_NOT_A_SECRET_SHAPE = re.compile(
    r"\A(?:"
    r"[/~.]|"  # a path
    r"[A-Za-z][A-Za-z0-9+.\-]*://|"  # a URL — its own rule handles the credentialled form
    r"[a-z_][a-z0-9_]*\Z|"  # pure lower snake_case: an identifier
    r"[A-Z_][A-Z0-9_]*\Z|"  # pure UPPER snake_case: a constant's name
    r"(?i:true|false|none|null)\b"
    r")"
)

# 16+ hex digits, or a long base64-ish run: no mixed case needed, the shape is the signal.
_HEX_SECRET = re.compile(r"\A[0-9a-fA-F]{16,}\Z")
_BASE64_SECRET = re.compile(r"\A[A-Za-z0-9+/]{20,}={0,2}\Z")

# `\n`, `\t`, `\"`… as the two literal characters, the way a jsonl stores them.
_JSON_ESCAPE = re.compile(r"\\[ntr\"'\\/bfu]")


def _looks_like_credential(value: str) -> bool:
    """Whether a value is plausible as an actual secret, not code or prose.

    Deliberately conservative, and calibrated against 60 MB of real transcripts: a name
    like ``token`` matches thousands of lines of ordinary programming talk, so what earns
    a warning is the *value* looking random — long, and either mixing character classes or
    having the shape of an encoded key.
    """
    value = value.strip("\"'`")
    if len(value) < 12:
        return False
    # A jsonl keeps its newlines and tabs as the two characters `\` + letter. A "value"
    # carrying one is a chunk of some tool's output that the regex sliced mid-line, not a
    # credential — this is what `grep -n` hits looked like (`3\tPassword:  4\tPassword:`).
    if _JSON_ESCAPE.search(value):
        return False
    if _HEX_SECRET.match(value) or _BASE64_SECRET.match(value):
        return True
    if _NOT_A_SECRET_SHAPE.match(value):
        return False
    classes = (
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() for c in value),
    )
    # Either visibly random, or long enough that two classes are still suspicious.
    return sum(classes) >= 3 or (len(value) >= 24 and sum(classes) >= 2)


def scan_text(text: str, path: Path, *, start_line: int = 1) -> list[Finding]:
    """Findings in ``text``, attributed to ``path``. Used directly by the tests.

    Every rule is tried on every line, not just the first that matches: a jsonl keeps a
    whole tool result on one physical line (its newlines are escaped), so "one finding per
    line" would report the database URL and quietly drop the token next to it. Findings
    from a line come back in the order they appear in the text, and identical values are
    reported once — an assignment that trips both the generic rule and a specific one is
    one secret, not two.
    """
    # (rule, raw value) -> Finding, so the same value seen again only bumps its counter.
    # Keyed on the raw value rather than the mask: two different keys sharing a prefix and
    # a length would otherwise collapse into one, hiding a secret. The raw value is a local
    # dict key and nothing more — what escapes this function is always masked.
    collected: dict[tuple[str, str], Finding] = {}
    for offset, raw_line in enumerate(text.splitlines()):
        line = raw_line[:MAX_LINE_CHARS]
        in_line: list[tuple[int, str, str]] = []  # (position, rule, raw value)
        seen: set[str] = set()
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                value = match.group(rule.group) if rule.group else match.group(0)
                if not value or value in seen:
                    continue
                if rule.group and _is_placeholder(value):
                    continue
                if rule.strict_value:
                    if not _looks_like_credential(value):
                        continue
                    # `\tPassword:` in a grep dump is a tab, not a variable called
                    # tPassword — a name preceded by a backslash comes from an escape.
                    if match.start() > 0 and line[match.start() - 1] == "\\":
                        continue
                seen.add(value)
                in_line.append((match.start(), rule.name, value))
        for _, name, value in sorted(in_line, key=lambda item: item[0]):
            key = (name, value)
            previous = collected.get(key)
            if previous is not None:
                collected[key] = replace(previous, occurrences=previous.occurrences + 1)
                continue
            collected[key] = Finding(
                path=path, line=start_line + offset, rule=name, excerpt=mask(value)
            )
            if len(collected) >= MAX_FINDINGS:
                return list(collected.values())
    return list(collected.values())


def redact(text: str, replacement: str | None = None) -> str:
    """``text`` with anything that looks like a credential taken out.

    For the places that echo transcript text back at the user — a session title in a
    report, say. A title is just the first prompt, and a first prompt can be
    ``export GITHUB_TOKEN=…``: masking the findings while printing the title verbatim
    would leak the secret through the label instead of the finding.

    By default each value becomes its :func:`mask`, which still identifies it. Pass
    ``replacement`` for a fixed marker where identifying it is not the point — a label
    reads better as ``export GITHUB_TOKEN=[credencial]``.
    """
    for value in _values_in(text):
        text = text.replace(value, replacement if replacement is not None else mask(value))
    return text


def _values_in(text: str) -> list[str]:
    """The raw matched values in one string, longest first so replacing is stable."""
    values: set[str] = set()
    for line in text.splitlines():
        for rule in RULES:
            for match in rule.pattern.finditer(line[:MAX_LINE_CHARS]):
                value = match.group(rule.group) if rule.group else match.group(0)
                if not value:
                    continue
                if rule.group and _is_placeholder(value):
                    continue
                if rule.strict_value and not _looks_like_credential(value):
                    continue
                values.add(value)
    return sorted(values, key=len, reverse=True)


def scan_files(paths: list[Path]) -> list[Finding]:
    """Scan every path, skipping what cannot be read as text.

    Unreadable or oversized files are skipped silently rather than raising: this runs on
    the way to a publish, and failing to scan must not fail the publish. The caller shows
    what it got; :func:`skipped_files` names what it could not look at.
    """
    findings: list[Finding] = []
    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        findings.extend(scan_text(text, path))
        if len(findings) >= MAX_FINDINGS:
            return findings[:MAX_FINDINGS]
    return findings


def skipped_files(paths: list[Path]) -> list[Path]:
    """Paths that :func:`scan_files` could not read, so the dialogue can say so.

    A file too big or not decodable as text is exactly where something could hide, so the
    honest answer is "not checked", not silence.
    """
    return [path for path in paths if _read_text(path) is None]


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:  # binary: an image or an archive, not something to grep
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")
