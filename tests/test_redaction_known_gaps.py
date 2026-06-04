# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Known false-negatives in the redaction layer.

Each test below documents a real-world string that SHOULD be redacted but
currently isn't. They're marked `xfail(strict=False)` so they stay in the
suite without breaking CI — but if/when somebody fixes the underlying regex,
the test flips to xpass and gets attention.

The point: surface the gaps so we know what we're not catching. Redaction
by regex is the right default, but it's the part most likely to fail
silently. Don't pretend the coverage is perfect.

NOTE on test strings: all secret-like values in this file are synthetic
placeholders. They contain `EXAMPLE` / `FAKE` / hyphens to defeat both
GitHub's secret scanner and any real-world pattern match. Real keys are
alphanumeric in tighter formats.

Workflow when you find a new gap in real captured data:
    1. Sanitize the offending string (replace any real-looking segment
       with `EXAMPLE` or hyphens).
    2. Add an xfail test here with a one-line reason.
    3. Later, if you tighten the regex, the test flips to xpass and CI
       reminds you to remove the xfail marker.
"""
import pytest

from cc_logger.redaction import redact_string


@pytest.fixture(autouse=True)
def _enable_redaction(monkeypatch):
    monkeypatch.setenv("REDACT_SECRETS", "1")


# ---------------------------------------------------------------------------
# Header variations the bearer-header regex misses
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="bearer-header requires the literal word 'Bearer'; misses 'Authorization: token <x>'")
def test_authorization_token_header_not_redacted():
    s = "Authorization: token EXAMPLE-token-fake-NOT-real-12345"
    assert "EXAMPLE-token-fake" not in redact_string(s)


@pytest.mark.xfail(reason="X-API-Key style headers aren't matched by any pattern")
def test_x_api_key_header_not_redacted():
    s = "X-API-Key: EXAMPLE-apikey-fake-NOT-real-67890"
    assert "EXAMPLE-apikey-fake" not in redact_string(s)


@pytest.mark.xfail(reason="Basic auth in URLs ('user:password@host') matches only for postgres scheme")
def test_basic_auth_non_postgres_url_not_redacted():
    s = "curl https://admin:EXAMPLE-pw-fake@api.example.com/data"
    assert "EXAMPLE-pw-fake" not in redact_string(s)


# ---------------------------------------------------------------------------
# Provider-specific token formats we don't have regex for
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="Stripe sk_live_/sk_test_ tokens use underscores; current sk-key pattern requires a hyphen after sk")
def test_stripe_secret_key_not_redacted():
    s = "STRIPE_SECRET=sk_live_EXAMPLE-fake-NOT-A-REAL-stripe-key"
    assert "sk_live_EXAMPLE" not in redact_string(s)


@pytest.mark.xfail(reason="Google API keys (AIza prefix) don't match any pattern")
def test_google_api_key_not_redacted():
    s = "GOOGLE_API_KEY=AIza-EXAMPLE-NOT-A-real-google-key-fake"
    assert "AIza-EXAMPLE" not in redact_string(s)


@pytest.mark.xfail(reason="Sendgrid keys (SG.xxx.yyy) not matched")
def test_sendgrid_key_not_redacted():
    s = "SENDGRID_API_KEY=SG.EXAMPLE-fake.NOT-a-real-sendgrid-key"
    assert "SG.EXAMPLE-fake" not in redact_string(s)


@pytest.mark.xfail(reason="Twilio account SIDs (AC...) and auth tokens not matched")
def test_twilio_credentials_not_redacted():
    s = "twilio --account-sid AC-EXAMPLE-not-real --auth-token EXAMPLE-twilio-token-fake"
    out = redact_string(s)
    assert "AC-EXAMPLE-not-real" not in out
    assert "EXAMPLE-twilio-token-fake" not in out


@pytest.mark.xfail(reason="JWTs (three base64 segments separated by dots) not matched")
def test_jwt_not_redacted():
    # Synthetic JWT-like shape; not a real signed token.
    s = "Cookie: session=EXAMPLE-header.EXAMPLE-payload.EXAMPLE-signature-fake"
    assert "EXAMPLE-header" not in redact_string(s)


# ---------------------------------------------------------------------------
# Generic secret patterns we'd want to catch but can't without false positives
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="Generic 'password: value' YAML/env shape not matched")
def test_yaml_password_field_not_redacted():
    s = "  password: EXAMPLE-yaml-secret-fake"
    assert "EXAMPLE-yaml-secret" not in redact_string(s)


@pytest.mark.xfail(reason="PII (emails) is intentionally NOT redacted by default — flag for review")
def test_email_not_redacted_by_default():
    s = "User created: jane.doe@example.com (id 42)"
    # If/when we add PII redaction, this should flip
    assert "jane.doe@example.com" not in redact_string(s)


@pytest.mark.xfail(reason="Short, high-entropy strings that look like tokens but match no specific pattern")
def test_generic_high_entropy_token_not_redacted():
    s = "API_TOKEN=EXAMPLE-high-entropy-fake-NOT-real-token"
    assert "EXAMPLE-high-entropy-fake" not in redact_string(s)


# ---------------------------------------------------------------------------
# Things that ARE caught (regression check — these should always pass).
# Lives here so the "what we catch" and "what we don't" sit side by side.
# ---------------------------------------------------------------------------

def test_anthropic_key_is_caught():
    """Sanity: redaction wiring still works. Failing this means the suite
    above is reporting false xfails (redaction broken globally)."""
    s = "sk-ant-abcdef1234567890_-1234567890abcdef"
    assert "[REDACTED" in redact_string(s)
