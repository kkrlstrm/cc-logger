"""Secret redaction + 50KB-per-field truncation with spillover into `artifacts`.

Two-step pipeline applied to tool_input/tool_response BEFORE write:
    1. redact() walks the payload and replaces secret patterns with markers.
    2. truncate() walks the (redacted) payload and replaces any string field
       over 50KB with {"_truncated_artifact_id": "<uuid>", ...}. The full
       (already-redacted) content goes to the artifacts table.

Doing redaction first means artifact rows never contain raw secrets either.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .redaction import redact

MAX_FIELD_BYTES = 50 * 1024  # 50 KB


@dataclass
class TruncationResult:
    payload: Any
    artifacts: list[dict] = field(default_factory=list)


def _measure(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return 0


def _truncate_recursive(
    node: Any,
    path: list[str],
    artifacts: list[dict],
) -> Any:
    if isinstance(node, dict):
        return {k: _truncate_recursive(v, path + [str(k)], artifacts) for k, v in node.items()}
    if isinstance(node, list):
        return [_truncate_recursive(v, path + [f"[{i}]"], artifacts) for i, v in enumerate(node)]
    if isinstance(node, str):
        size = _measure(node)
        if size > MAX_FIELD_BYTES:
            artifact_id = str(uuid.uuid4())
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "field_name": ".".join(path) or "(root)",
                    "full_content": node,
                    "size_bytes": size,
                }
            )
            return {
                "_truncated_artifact_id": artifact_id,
                "_truncated_size_bytes": size,
                "_truncated_field": ".".join(path) or "(root)",
            }
    return node


def truncate(payload: Any) -> TruncationResult:
    """Redact secrets, then walk payload replacing oversized strings with markers.

    Returns the rewritten payload plus a list of artifact rows to insert.
    """
    redacted = redact(payload)
    artifacts: list[dict] = []
    rewritten = _truncate_recursive(redacted, [], artifacts)
    return TruncationResult(payload=rewritten, artifacts=artifacts)
