import pytest

from relay.redact import redact, redact_text


@pytest.mark.parametrize(
    ("text", "secret", "label"),
    [
        ("contact jane.doe@example.com now", "jane.doe@example.com", "email"),
        ("Authorization: Bearer abcDEF123456.xyz", "abcDEF123456.xyz", "bearer_token"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl here", "eyJhbGciOiJIUzI1NiJ9", "jwt"),
        ("key dp_FAKEdemoKEY7f3a9c2e1b4d used", "dp_FAKEdemoKEY7f3a9c2e1b4d", "api_key"),
        ("GROQ gsk_abcdefghijklmnopqrstu", "gsk_abcdefghijklmnopqrstu", "api_key"),
        ("aws AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", "api_key"),
        ("postgres://reporter:Rep0rt!ng2026@db:5432/orders", "Rep0rt!ng2026", "url_password"),
        ('password="hunter22" retry', "hunter22", "secret_assignment"),
        ("client_secret: abcd1234efgh", "abcd1234efgh", "secret_assignment"),
        ("card 4242 4242 4242 4242 declined", "4242 4242 4242 4242", "card_number"),
    ],
)
def test_redacts_sensitive_values(text, secret, label):
    result = redact(text)
    assert secret not in result.text
    assert f"[REDACTED:{label}]" in result.text
    assert result.counts.get(label, 0) >= 1


def test_keeps_ordinary_text_and_numbers():
    text = "payments-api 5xx 14.8% at 2026-10-14T14:02Z order 100482211 request_id=8f2c1 latency=212ms"
    assert redact_text(text) == text


def test_long_number_failing_luhn_is_not_a_card():
    assert redact_text("trace 1234567890123456") == "trace 1234567890123456"


def test_keeps_context_around_secret():
    assert redact_text("db postgres://reporter:pw123@orders-db-2:5432/orders") == (
        "db postgres://reporter:[REDACTED:url_password]@orders-db-2:5432/orders"
    )


def test_is_idempotent():
    once = redact_text("Bearer abcdefgh12345678 api_key=dp_FAKEdemoKEY7f3a9c2e1b4d a@b.io")
    assert redact_text(once) == once
    assert redact(once).counts == {}
