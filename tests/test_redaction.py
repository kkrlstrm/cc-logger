import os
import pytest

from cc_logger.redaction import redact, redact_string


@pytest.fixture(autouse=True)
def _enable_redaction(monkeypatch):
    monkeypatch.setenv("REDACT_SECRETS", "1")


def test_anthropic_key_redacted():
    s = "curl -H 'x-api-key: sk-ant-abc1234567890XYZ_-abcdef' https://api.anthropic.com"
    out = redact_string(s)
    assert "sk-ant-abc1234567890XYZ_-abcdef" not in out
    assert "[REDACTED:anthropic-or-openai-key]" in out


def test_openai_key_redacted():
    s = "OPENAI_API_KEY=sk-proj-abc1234567890XYZ_-abcdef"
    out = redact_string(s)
    assert "sk-proj-abc1234567890XYZ_-abcdef" not in out


def test_github_token_redacted():
    s = "Authorization: token ghp_aabbccddeeff112233445566778899aabbccdd1122"
    out = redact_string(s)
    assert "[REDACTED:github-token]" in out


def test_gitlab_token_redacted():
    s = "glpat-abcdefghijk1234567890_-"
    out = redact_string(s)
    assert "[REDACTED:gitlab-token]" in out


def test_neon_password_redacted():
    s = "postgresql://user:npg_abc123XYZ@host/db"
    out = redact_string(s)
    # url-password OR neon-password — either firing is fine
    assert "npg_abc123XYZ" not in out


def test_postgres_password_redacted():
    s = "postgresql://user:supersecret@host:5432/db"
    out = redact_string(s)
    assert "supersecret" not in out
    assert "postgresql://user:" in out  # prefix preserved
    assert "@host" in out  # suffix preserved


def test_bearer_header_redacted():
    s = "Bearer abc1234567890ABCDEF_-xyz"
    out = redact_string(s)
    assert "[REDACTED:bearer-header]" in out


def test_aws_key_redacted():
    s = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    out = redact_string(s)
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_url_password_query_param():
    s = "https://api.example.com/data?api_key=verysecretvalue123&q=hello"
    out = redact_string(s)
    assert "verysecretvalue123" not in out
    assert "api_key=" in out  # prefix preserved
    assert "q=hello" in out  # other params preserved


def test_recursive_redaction_dict():
    payload = {
        "command": "curl -H 'Bearer abc1234567890ABCDEF_-xyz' https://x.com",
        "nested": {"token": "ghp_aabbccddeeff112233445566778899aabbccdd1122"},
        "list": ["sk-ant-abc1234567890XYZ_-abcdef"],
        "safe": 42,
    }
    out = redact(payload)
    assert "abc1234567890ABCDEF" not in str(out["command"])
    assert "ghp_aa" not in str(out["nested"]["token"])
    assert "[REDACTED" in out["list"][0]
    assert out["safe"] == 42


def test_redaction_disabled(monkeypatch):
    # The env var bypass is checked by redact(), not redact_string().
    monkeypatch.setenv("REDACT_SECRETS", "0")
    s = "Bearer abc1234567890ABCDEF_-xyz"
    assert redact(s) == s
    assert redact({"x": s}) == {"x": s}


def test_no_false_positive_on_safe_strings():
    # Short strings or non-pattern content should pass through unchanged
    assert redact_string("hello world") == "hello world"
    assert redact_string("sk-short") == "sk-short"  # too short to match
    assert redact_string("Bearer foo") == "Bearer foo"  # too short
