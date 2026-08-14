"""Deterministic secret classification (R6.1A) — the gate before every provider.

Pure local code. This module MUST NOT use: an LLM, embeddings, the web,
connectors, or any external API. It is imported on hot paths (every note,
every ingested document, every tool result), so all patterns are compiled once
at import time and matching is linear in the length of the text.

API contract — the part that matters most:
    the scanner NEVER returns, logs, stores or hashes a matched value.
    Callers get categories and counts. An "excerpt for debugging" would
    reintroduce exactly the leak this module exists to stop, and a hash of a
    short secret is a reversible encoding (it can be brute-forced), so neither
    is available anywhere in this API.

What is a HARD secret here: something that grants access on its own
(password, API key, OAuth/bearer token, private key, session cookie, recovery
code list, seed phrase). Identity is NOT a secret: usernames, e-mail
addresses, URLs, phone numbers, IBAN/ЄДРПОУ/ІПН, invoice and order numbers
and ordinary bank requisites stay searchable — DAN.OS is a business assistant,
not a redaction machine. False positives cost Danylo real answers, so every
value-style rule is guarded by a concreteness check (see _is_concrete_value).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

SCANNER_VERSION = 1

# Large documents are scanned in overlapping windows so that a secret sitting
# on a window boundary is still seen. NOT a prefix scan: every section of the
# document is examined (the DMC-workbook lesson — the tail is where the
# credential blocks live).
SECTION_CHARS = 20_000
SECTION_OVERLAP = 1_024
# Verdict-preserving bound: once this many distinct findings are collected the
# document is already blocked and no further match can change the outcome.
MAX_COUNTED_FINDINGS = 500

MIN_VALUE_LEN = 4
MAX_VALUE_LEN = 200
MIN_COOKIE_LEN = 8


class SecretCategory(StrEnum):
    PASSWORD = "password"
    API_KEY = "api_key"
    OAUTH_TOKEN = "oauth_token"
    BEARER_TOKEN = "bearer_token"
    PRIVATE_KEY = "private_key"
    SESSION_COOKIE = "session_cookie"
    RECOVERY_CODE = "recovery_code"
    SEED_PHRASE = "seed_phrase"


@dataclass(frozen=True)
class SecretScanResult:
    blocked: bool
    categories: tuple[SecretCategory, ...]
    finding_count: int

    def as_meta(self) -> dict:
        """Metadata-safe projection for persistence/audit (no values, ever)."""
        return {"categories": [str(c) for c in self.categories],
                "finding_count": self.finding_count,
                "scanner_version": SCANNER_VERSION}


CLEAN = SecretScanResult(blocked=False, categories=(), finding_count=0)


# ---------- normalisation ----------

_WHITESPACE_MAP = {ord(c): " " for c in (
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u202f\u205f\u3000\u0085\v\f")}
_ZERO_WIDTH_MAP = {ord(c): None for c in
                   "\u200b\u200c\u200d\u2060\ufeff\u00ad"}


def _normalise(text: str) -> str:
    """NFKC + unicode whitespace folding.

    Without this, `ｐａｓｓｗｏｒｄ：x` or `password: x` would slip past
    ASCII patterns — evasion that costs an attacker nothing.
    """
    text = unicodedata.normalize("NFKC", text)
    return text.translate(_ZERO_WIDTH_MAP).translate(_WHITESPACE_MAP)


# ---------- value concreteness (false-positive control) ----------

_PLACEHOLDERS = {
    "password", "passwd", "pwd", "pass", "пароль", "паролі", "паролю",
    "secret", "секрет", "token", "токен", "apikey", "api_key", "api-key",
    "your_api_key", "yourapikey", "your_token", "your_password",
    "redacted", "hidden", "masked", "приховано", "видалено", "removed",
    "changeme", "change_me", "example", "sample", "placeholder", "todo",
    "none", "null", "nil", "n/a", "na", "empty", "unset", "test", "dummy",
    "нема", "немає", "невідомо", "невідомий", "тут", "-", "—",
}
# words that follow "password:" in a POLICY sentence, not a credential dump
_POLICY_WORDS = {
    "minimum", "min", "max", "maximum", "policy", "required", "requirement",
    "must", "should", "complex", "complexity", "length", "characters",
    "chars", "symbols", "rotate", "rotation", "manager", "vault", "expires",
    "expiry", "reset", "unique", "strong",
}
_CYRILLIC_WORD_RE = re.compile(r"^[А-Яа-яЁёЇїІіЄєҐґЫыЭэЪъЬь'’ʼ-]+$")
_URL_PREFIXES = ("http://", "https://", "ftp://", "www.")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _is_concrete_value(raw: str) -> bool:
    """True when the captured token really looks like a credential value.

    Everything rejected here is a deliberate false-positive control: policy
    prose, placeholders, e-mails, URLs and masked values must stay indexable.
    """
    v = raw.strip().strip("`\"'«»“”")
    v = v.rstrip(".,;:!?)]}»")
    if not (MIN_VALUE_LEN <= len(v) <= MAX_VALUE_LEN):
        return False
    low = v.lower()
    if low in _PLACEHOLDERS or low in _POLICY_WORDS:
        return False
    if v[0] in "<[{$%" or low.startswith(("your_", "your-", "&lt;")):
        return False
    if low.startswith(_URL_PREFIXES):
        return False
    if _EMAIL_RE.match(v):          # a login is identity, not a secret
        return False
    if len(set(v)) <= 2:            # ***, xxxx, ------, 0000
        return False
    if _CYRILLIC_WORD_RE.match(v):  # «Пароль — це важливо» / «пароль: змінено»
        return False
    return True


def _is_concrete_cookie(raw: str) -> bool:
    v = raw.strip().strip("`\"'").rstrip(".,;")
    return len(v) >= MIN_COOKIE_LEN and _is_concrete_value(v)


# ---------- structural patterns (the FORMAT is the secret) ----------

_NB = r"(?<![A-Za-z0-9_\-])"   # no-boundary guards that respect '-' and '_'
_NA = r"(?![A-Za-z0-9_\-])"

_STRUCTURAL: tuple[tuple[SecretCategory, re.Pattern[str]], ...] = (
    (SecretCategory.PRIVATE_KEY,
     re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    (SecretCategory.PRIVATE_KEY,
     re.compile(r"\bPuTTY-User-Key-File-\d")),
    # OpenAI / Anthropic
    (SecretCategory.API_KEY,
     re.compile(_NB + r"sk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{20,}" + _NA)),
    # GitHub
    (SecretCategory.API_KEY,
     re.compile(_NB + r"gh[pousr]_[A-Za-z0-9]{30,}" + _NA)),
    (SecretCategory.API_KEY,
     re.compile(_NB + r"github_pat_[A-Za-z0-9_]{40,}" + _NA)),
    # Google API key
    (SecretCategory.API_KEY,
     re.compile(_NB + r"AIza[A-Za-z0-9_\-]{30,}" + _NA)),
    # Slack
    (SecretCategory.API_KEY,
     re.compile(_NB + r"xox[baprse]-[A-Za-z0-9\-]{10,}" + _NA)),
    # AWS access key id
    (SecretCategory.API_KEY,
     re.compile(_NB + r"(?:AKIA|ASIA)[0-9A-Z]{16}" + _NA)),
    # Telegram bot token
    (SecretCategory.API_KEY,
     re.compile(_NB + r"\d{8,12}:[A-Za-z0-9_\-]{35}" + _NA)),
    # Google OAuth client secret
    (SecretCategory.OAUTH_TOKEN,
     re.compile(_NB + r"GOCSPX-[A-Za-z0-9_\-]{15,}" + _NA)),
    # JWT — a bearer credential by construction
    (SecretCategory.BEARER_TOKEN,
     re.compile(_NB + r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
                r"\.[A-Za-z0-9_\-]{8,}" + _NA)),
)

# ---------- field patterns (KEY + concrete VALUE) ----------

_VALUE = r"([^\s\"',;}{\]\[]+)"

_FIELDS: tuple[tuple[SecretCategory, re.Pattern[str]], ...] = (
    (SecretCategory.BEARER_TOKEN,
     re.compile(r"authorization\s*[:=]\s*bearer\s+" + _VALUE, re.IGNORECASE)),
    (SecretCategory.BEARER_TOKEN,
     re.compile(r"\bbearer[ _\-]?token\s*[:=]\s*[\"']?" + _VALUE, re.IGNORECASE)),
    (SecretCategory.OAUTH_TOKEN,
     re.compile(r"\b(?:access|refresh|id)[ _\-]?token\s*[\"']?\s*[:=]\s*[\"']?"
                + _VALUE, re.IGNORECASE)),
    (SecretCategory.OAUTH_TOKEN,
     re.compile(r"\bclient[ _\-]?secret\s*[\"']?\s*[:=]\s*[\"']?" + _VALUE,
                re.IGNORECASE)),
    (SecretCategory.API_KEY,
     re.compile(r"\b(?:api[ _\-]?key|apikey|api[ _\-]?secret|api[ _\-]?token|"
                r"ключ\s+api|токен\s+доступу)\s*[\"']?\s*[:=]\s*[\"']?" + _VALUE,
                re.IGNORECASE)),
    (SecretCategory.PASSWORD,
     re.compile(r"(?:\bpassword|\bpasswd|\bpwd|\bpass|пароль|парол[ья])"
                r"\s*(?:[:=]|\s[-—–]\s)\s*[\"']?" + _VALUE, re.IGNORECASE)),
    (SecretCategory.PASSWORD,
     re.compile(r"\bsecret\s*[\"']?\s*[:=]\s*[\"']?" + _VALUE, re.IGNORECASE)),
    (SecretCategory.SESSION_COOKIE,
     re.compile(r"\b(?:session[ _\-]?id|sessionid|phpsessid|jsessionid|"
                r"connect\.sid|csrf[ _\-]?token|xsrf[ _\-]?token|"
                r"auth[ _\-]?cookie|remember[ _\-]?token|session[ _\-]?key)"
                r"\s*[\"']?\s*[:=]\s*[\"']?" + _VALUE, re.IGNORECASE)),
    (SecretCategory.SESSION_COOKIE,
     re.compile(r"\bcookie\s*:\s*[^\s;=]+=" + _VALUE, re.IGNORECASE)),
)

_COOKIE_CATEGORIES = {SecretCategory.SESSION_COOKIE}

# Cheap literal pre-filters. A 2 MB workbook of hotel rows contains none of
# these, so the expensive alternations never run on it — the scan stays fast
# enough to sit on the ingest path without changing what it finds.
_FIELD_TRIGGER_RE = re.compile(
    r"pass|pwd|парол|secret|секрет|token|токен|key|ключ|cookie|session|sid|"
    r"bearer|authorization|seed", re.IGNORECASE)
_RECOVERY_TRIGGER_RE = re.compile(r"cod|код|парол", re.IGNORECASE)

# ---------- credential TABLES (header row + value rows) ----------
#
# The shape that started all of this: a spreadsheet with a «Пароль» column.
# The key and the value never sit on the same line, so every field rule above
# misses them. Here the header cell names the column and the cells beneath it
# are checked positionally.

_CELL_SPLIT_RE = re.compile(r"\s*[|;\t]\s*")
_HEADER_CATEGORIES: tuple[tuple[SecretCategory, frozenset[str]], ...] = (
    (SecretCategory.PASSWORD, frozenset({
        "пароль", "паролі", "пароль доступу", "password", "passwords",
        "passwd", "pwd", "pass", "секрет", "secret"})),
    (SecretCategory.API_KEY, frozenset({
        "токен", "token", "api key", "api-key", "api_key", "apikey",
        "api token", "ключ api", "access key"})),
    (SecretCategory.SESSION_COOKIE, frozenset({"cookie", "session", "сесія"})),
    (SecretCategory.SEED_PHRASE, frozenset({
        "seed", "seed phrase", "мнемоніка", "сід-фраза", "сід фраза"})),
    (SecretCategory.PRIVATE_KEY, frozenset({
        "private key", "приватний ключ", "закритий ключ"})),
)
_HEADER_LOOKUP = {name: cat for cat, names in _HEADER_CATEGORIES for name in names}
_TABLE_LOOKAHEAD = 60


def _table_hits(text: str, offset: int) -> list[tuple[SecretCategory, int]]:
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    hits: list[tuple[SecretCategory, int]] = []
    for i, line in enumerate(lines):
        cells = _CELL_SPLIT_RE.split(line.strip())
        if len(cells) < 2:
            continue
        columns = {idx: _HEADER_LOOKUP[c.strip().lower()]
                   for idx, c in enumerate(cells)
                   if c.strip().lower() in _HEADER_LOOKUP}
        if not columns:
            continue
        for j in range(i + 1, min(i + 1 + _TABLE_LOOKAHEAD, len(lines))):
            row = _CELL_SPLIT_RE.split(lines[j].strip())
            if len(row) < 2:
                continue
            for idx, category in columns.items():
                if idx >= len(row):
                    continue
                value = row[idx].strip()
                # a credential has no spaces; a description does
                if (value and " " not in value
                        and value.lower() not in _HEADER_LOOKUP
                        and _is_concrete_value(value)):
                    hits.append((category, offset + starts[j]))
    return hits


# ---------- recovery codes ----------

_RECOVERY_MARKER_RE = re.compile(
    r"(?:recovery|backup|one[ \-]?time|two[ \-]?factor|2fa|"
    r"резервн\w*|відновлен\w*|запасн\w*|одноразов\w*)[\s\-]*"
    r"(?:codes?|код\w*|парол\w*)", re.IGNORECASE)
_RECOVERY_WINDOW = 400
_CODE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9\-]{4,15}\b")
_MIN_RECOVERY_CODES = 3

# ---------- seed phrases ----------

_MNEMONIC_MARKER_RE = re.compile(
    r"seed[ \-]?phrase|seed[ \-]?words|mnemonic|recovery phrase|"
    r"с[иі]д[ \-]?фраз\w*|мнемоні\w*|секретна фраза", re.IGNORECASE)
_MNEMONIC_LENGTHS = {12, 15, 18, 21, 24}
_ENGLISH_STOP = {
    "the", "and", "for", "that", "this", "with", "you", "are", "not", "but",
    "have", "has", "had", "was", "were", "from", "they", "their", "been",
    "will", "would", "could", "should", "which", "when", "what", "where",
    "who", "your", "our", "its", "his", "her", "them", "then", "than",
    "there", "here", "also", "just", "only", "some", "any", "all", "can",
    "may", "into", "over", "such", "very", "each", "how", "why", "does",
}


_RUN_TOKEN_RE = re.compile(r"[A-Za-z]+")


def _word_runs(text: str) -> list[tuple[int, list[str]]]:
    """Maximal runs of >=12 lowercase 3..8-letter words (BIP39 word shape).

    Returns (start offset of the run, words) so that a run seen twice through
    overlapping windows is counted once.
    """
    runs: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start = 0
    prev_end = -1
    for m in _RUN_TOKEN_RE.finditer(text):
        tok = m.group(0)
        gap_clean = prev_end < 0 or not text[prev_end:m.start()].strip(" \n\r\t")
        ok = tok.islower() and 3 <= len(tok) <= 8 and (gap_clean or not current)
        if ok:
            if not current:
                start = m.start()
            current.append(tok)
        else:
            if len(current) >= 12:
                runs.append((start, current))
            current = [tok] if (tok.islower() and 3 <= len(tok) <= 8) else []
            start = m.start() if current else start
        prev_end = m.end()
    if len(current) >= 12:
        runs.append((start, current))
    return runs


def _seed_hits(text: str, offset: int) -> list[tuple[SecretCategory, int]]:
    """A labelled mnemonic, or an unlabelled run with BIP39 shape.

    The unlabelled rule is deliberately strict (exact 12/15/18/21/24 words, no
    English function words, near-unique tokens) so that ordinary prose and
    word lists are not mistaken for a wallet phrase.
    """
    runs = _word_runs(text)
    if not runs:
        return []
    labelled = bool(_MNEMONIC_MARKER_RE.search(text))
    hits: list[tuple[SecretCategory, int]] = []
    for start, run in runs:
        if labelled or (len(run) in _MNEMONIC_LENGTHS
                        and not (set(run) & _ENGLISH_STOP)
                        and len(set(run)) >= len(run) - 1):
            hits.append((SecretCategory.SEED_PHRASE, offset + start))
    return hits


def _recovery_hits(text: str, offset: int) -> list[tuple[SecretCategory, int]]:
    hits: list[tuple[SecretCategory, int]] = []
    for m in _RECOVERY_MARKER_RE.finditer(text):
        window = text[m.end():m.end() + _RECOVERY_WINDOW]
        codes = 0
        for tok in _CODE_TOKEN_RE.findall(window):
            bare = tok.replace("-", "")
            if any(c.isdigit() for c in bare) and any(c.isalpha() for c in bare):
                codes += 1
            elif "-" in tok and bare.isdigit() and len(bare) >= 8:
                codes += 1
        if codes >= _MIN_RECOVERY_CODES:
            hits.append((SecretCategory.RECOVERY_CODE, offset + m.start()))
    return hits


# ---------- scanning ----------

def _section_hits(section: str, offset: int) -> list[tuple[SecretCategory, int]]:
    """(category, absolute start) for every match in one window."""
    hits: list[tuple[SecretCategory, int]] = []
    for category, pattern in _STRUCTURAL:
        for m in pattern.finditer(section):
            hits.append((category, offset + m.start()))
    if _FIELD_TRIGGER_RE.search(section):
        for category, pattern in _FIELDS:
            check = (_is_concrete_cookie if category in _COOKIE_CATEGORIES
                     else _is_concrete_value)
            for m in pattern.finditer(section):
                if check(m.group(1)):
                    hits.append((category, offset + m.start()))
        hits.extend(_table_hits(section, offset))
    if _RECOVERY_TRIGGER_RE.search(section):
        hits.extend(_recovery_hits(section, offset))
    hits.extend(_seed_hits(section, offset))
    return hits


def _sections(text: str):
    """Overlapping windows covering the WHOLE text (never a prefix only)."""
    if len(text) <= SECTION_CHARS:
        yield text, 0
        return
    step = SECTION_CHARS - SECTION_OVERLAP
    for start in range(0, len(text), step):
        yield text[start:start + SECTION_CHARS], start
        if start + SECTION_CHARS >= len(text):
            return


def scan_text(text: str) -> SecretScanResult:
    """Classify text. Deterministic, local, value-free output."""
    if not text or not text.strip():
        return CLEAN
    normalised = _normalise(text)
    seen: set[tuple[SecretCategory, int]] = set()
    for section, offset in _sections(normalised):
        for hit in _section_hits(section, offset):
            seen.add(hit)
        if len(seen) >= MAX_COUNTED_FINDINGS:
            break  # already blocked — no later match can change the verdict
    if not seen:
        return CLEAN
    categories = tuple(sorted({c for c, _ in seen}, key=str))
    return SecretScanResult(blocked=True, categories=categories,
                            finding_count=len(seen))


def scan_parts(*parts: str | None) -> SecretScanResult:
    """Scan several fields of one resource as a single unit."""
    joined = "\n".join(p for p in parts if p)
    return scan_text(joined)
