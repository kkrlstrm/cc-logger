# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regex-based redaction of common secret patterns.

Applied to tool_input and tool_response BEFORE they're written to the DB
(and BEFORE truncation, so the artifact spillover doesn't leak secrets either).

Catches the obvious 90%: API keys, GitHub/GitLab/Slack tokens, AWS keys,
bearer tokens, and passwords embedded in Postgres connection strings. Not
a substitute for not putting secrets in Bash commands in the first place,
but a sensible default for a tool that logs everything Claude Code runs.

Disabled via REDACT_SECRETS=0 in the environment.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Each pattern: (name, compiled regex, secret_group_number).
# secret_group_number=0 → replace the entire match with [REDACTED:<name>].
# secret_group_number=N → replace only that capture group, keep all others.
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("anthropic-or-openai-key", re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("github-token", re.compile(r"gh[psouar]_[A-Za-z0-9]{36,}"), 0),
    ("gitlab-token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"), 0),
    ("neon-password", re.compile(r"npg_[A-Za-z0-9]+"), 0),
    ("slack-token", re.compile(r"xox[bpasr]-[A-Za-z0-9\-]{10,}"), 0),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}"), 0),
    ("bearer-header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"), 0),
    # Postgres connection strings with embedded password: keep prefix and @ delimiter,
    # redact only the password (group 2).
    (
        "postgres-password",
        re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)([^@/\s]+)(@)"),
        2,
    ),
    # Common "key=value" pairs in URLs and query strings — redact only the value (group 2).
    (
        "url-password",
        re.compile(
            r"([?&](?:password|passwd|pwd|secret|api_key|apikey|token|access_token)=)([^&\s\"']+)",
            re.IGNORECASE,
        ),
        2,
    ),
]


def _redaction_enabled() -> bool:
    return os.getenv("REDACT_SECRETS", "1") not in ("0", "false", "no")


def _make_replacer(name: str, regex: re.Pattern[str], secret_group: int):
    """Build a re.sub replacement function for a given pattern."""
    if secret_group == 0:
        return f"[REDACTED:{name}]"

    def repl(m: re.Match[str]) -> str:
        parts = []
        for i in range(1, regex.groups + 1):
            if i == secret_group:
                parts.append(f"[REDACTED:{name}]")
            else:
                parts.append(m.group(i) or "")
        return "".join(parts)
    return repl


def redact_string(s: str) -> str:
    """Apply every redaction pattern to a single string. Does NOT check the env
    var — use `redact()` if you want REDACT_SECRETS=0 to bypass.
    """
    if not isinstance(s, str) or not s:
        return s
    for name, regex, secret_group in _PATTERNS:
        s = regex.sub(_make_replacer(name, regex, secret_group), s)
    return s


def redact(value: Any) -> Any:
    """Recursively redact strings inside a JSON-shaped value.

    Returns the input unchanged if REDACT_SECRETS=0.
    """
    if not _redaction_enabled():
        return value
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
