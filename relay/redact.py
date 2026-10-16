"""Scrub secrets, tokens and personal data from text before it reaches an LLM or the database.

Patterns are ordered from most to least specific so a JWT inside "Authorization: Bearer ..." is
reported once, as a bearer token, rather than partially matched by a later rule.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

_PLACEHOLDER = "[REDACTED:{}]"

# (label, pattern, group to replace; 0 = whole match)
_RULES: list[tuple[str, re.Pattern[str], int]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        0,
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{8,})"), 1),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), 0),
    ("url_password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s@/]+)@", re.I), 1),
    (
        "api_key",
        re.compile(
            r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"  # Stripe-style
            r"|\bsk-[A-Za-z0-9_-]{16,}"  # OpenAI-style
            r"|\bgsk_[A-Za-z0-9]{16,}"  # Groq
            r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
            r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"  # Slack
            r"|\bAKIA[0-9A-Z]{16}\b"  # AWS access key id
            r"|\bdp_[A-Za-z0-9_-]{8,}"  # DocPilot
        ),
        0,
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?key|auth[_-]?token"
            r"|token|session[_-]?id|cookie)\b\s*[\"']?\s*[:=]\s*[\"']?([^\s\"',;&]{4,})"
        ),
        1,
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 0),
    ("card_number", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), 0),
]


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class Redaction:
    text: str
    counts: dict[str, int] = field(default_factory=dict)


def redact(text: str) -> Redaction:
    counts: Counter[str] = Counter()

    for label, pattern, group in _RULES:

        def replace(m: re.Match[str], label: str = label, group: int = group) -> str:
            secret = m.group(group)
            if label == "card_number" and not _luhn_ok(secret):
                return m.group(0)  # order ids, timestamps and other long numbers are fine
            if secret.startswith("[REDACTED:"):
                return m.group(0)
            counts[label] += 1
            start, end = m.start(group) - m.start(0), m.end(group) - m.start(0)
            whole = m.group(0)
            return whole[:start] + _PLACEHOLDER.format(label) + whole[end:]

        text = pattern.sub(replace, text)

    return Redaction(text, dict(counts))


def redact_text(text: str) -> str:
    return redact(text).text
