# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from cc_logger.artifacts import truncate, MAX_FIELD_BYTES


@pytest.fixture(autouse=True)
def _disable_redaction(monkeypatch):
    # Isolate artifact behavior from redaction
    monkeypatch.setenv("REDACT_SECRETS", "0")


def test_no_truncation_for_small_payload():
    payload = {"command": "ls -la", "stdout": "a small string"}
    result = truncate(payload)
    assert result.payload == payload
    assert result.artifacts == []


def test_oversized_string_spills_to_artifact():
    big = "x" * (MAX_FIELD_BYTES + 1000)
    payload = {"stdout": big, "exit_code": 0}
    result = truncate(payload)
    # The original string is replaced with a marker dict
    assert isinstance(result.payload["stdout"], dict)
    assert "_truncated_artifact_id" in result.payload["stdout"]
    assert result.payload["stdout"]["_truncated_size_bytes"] == len(big.encode("utf-8"))
    assert result.payload["exit_code"] == 0
    # One artifact row is queued
    assert len(result.artifacts) == 1
    assert result.artifacts[0]["full_content"] == big
    assert result.artifacts[0]["size_bytes"] == len(big.encode("utf-8"))


def test_multiple_oversized_fields():
    big_a = "a" * (MAX_FIELD_BYTES + 100)
    big_b = "b" * (MAX_FIELD_BYTES + 200)
    payload = {"stdout": big_a, "stderr": big_b}
    result = truncate(payload)
    assert len(result.artifacts) == 2
    field_names = {a["field_name"] for a in result.artifacts}
    assert field_names == {"stdout", "stderr"}


def test_nested_oversized_field():
    big = "z" * (MAX_FIELD_BYTES + 50)
    payload = {"outer": {"inner": {"deep": big}}}
    result = truncate(payload)
    assert len(result.artifacts) == 1
    assert result.artifacts[0]["field_name"] == "outer.inner.deep"


def test_list_oversized_element():
    big = "q" * (MAX_FIELD_BYTES + 1)
    payload = {"items": ["small", big, "small2"]}
    result = truncate(payload)
    assert len(result.artifacts) == 1
    assert result.artifacts[0]["field_name"] == "items.[1]"


def test_payload_below_threshold():
    payload = {"x": "y" * MAX_FIELD_BYTES}  # exactly the threshold
    result = truncate(payload)
    assert result.artifacts == []
    assert result.payload == payload


def test_non_string_payload():
    payload = {"count": 12345, "flag": True, "ratio": 0.5}
    result = truncate(payload)
    assert result.payload == payload
    assert result.artifacts == []
