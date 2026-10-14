"""Credential detection: what it must catch, and what it must leave alone.

The second half matters as much as the first. This runs over an archive of
messages, agent transcripts, and multilingual chat, and a detector that eats
identifiers, hashes, or ordinary sentences would quietly hollow out the
evidence the product is built to show. So every negative case here is a real
shape from that corpus, not a strawman.
"""

from __future__ import annotations

import base64

import pytest

from varyz.privacy.secrets_scan import MAX_SCAN_CHARS, redact

CAUGHT = [
    ("Anthropic API key", "sk-ant-api03-" + "A" * 30),
    ("OpenAI API key", "sk-proj-" + "b" * 40),
    ("GitHub token", "ghp_" + "c" * 36),
    ("GitHub fine-grained token", "github_pat_" + "d" * 30),
    ("Slack token", "xoxb-123456789012-abcdefghijklmnop"),
    ("Google API key", "AIza" + "E" * 35),
    ("AWS access key id", "AKIA" + "F" * 16),
    ("Stripe key", "sk_live_" + "g" * 24),
    ("JSON web token", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVP1234"),
]


@pytest.mark.parametrize(("kind", "value"), CAUGHT)
def test_a_credential_is_withheld_and_named(kind, value):
    text, removed = redact(f"the key is {value} ok?")
    assert value not in text
    assert f"[{kind} removed]" in text
    assert removed == (kind,)
    # The sentence around it survives — the user has to be able to read the
    # evidence, only not the secret.
    assert text.startswith("the key is ")
    assert text.endswith(" ok?")


def test_assignments_keep_their_label_and_lose_their_value():
    text, removed = redact("PASSWORD=hunter2000 and api_key: abc123def456")
    assert "hunter2000" not in text
    assert "abc123def456" not in text
    assert "PASSWORD=" in text
    assert removed == ("password",)


def test_a_bearer_header_loses_only_the_token():
    text, _ = redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in text
    assert "Bearer" in text


def test_credentials_in_a_url_lose_the_password_not_the_host():
    text, removed = redact("clone https://egor:s3cr3tpw@github.com/x/y.git")
    assert "s3cr3tpw" not in text
    assert "github.com" in text
    assert "egor" in text
    assert removed == ("credentials in a URL",)


def test_a_private_key_block_goes_whole():
    block = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAA\nAAAA\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    text, removed = redact(f"here:\n{block}\nthanks")
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in text
    assert removed == ("private key",)
    assert text.startswith("here:\n") and text.endswith("\nthanks")


def test_a_truncated_private_key_block_withholds_the_unbounded_tail():
    value = "-----BEGIN PRIVATE KEY-----\n" + "cHJpdmF0ZS1rZXk="

    text, removed = redact(f"broken export:\n{value}")

    assert "cHJpdmF0ZS1rZXk=" not in text
    assert removed == ("malformed private key",)


def test_a_card_number_is_caught_only_when_it_checks_out():
    caught, removed = redact("card 4111 1111 1111 1111 expires soon")
    assert "4111" not in caught
    assert removed == ("payment card number",)

    # Same length, fails Luhn: an order number, not a card.
    kept, nothing = redact("order 1234 5678 9012 3456 shipped")
    assert "1234 5678 9012 3456" in kept
    assert nothing == ()


NOT_SECRETS = [
    "I believe software should stay local and private.",
    "Я думаю, что честное измерение важнее уверенных догадок.",
    "commit 57eb8e7f2a1c9d4e8b6a0f3c5d7e9a1b3c5d7e9f",
    "entry 01KZ44ZJJ5F46AC6QCSNSYCK0G",
    "See reports/RESOURCE_COST_2026-08-05.md for the numbers",
    "my phone is +49 151 23456789",
    "the run took 2 h 33 min and dropped to 2 min 38 s",
    "sha256:1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606",
]


@pytest.mark.parametrize("text", NOT_SECRETS)
def test_ordinary_archive_text_is_untouched(text):
    out, removed = redact(text)
    assert out == text
    assert removed == ()


def test_overlapping_matches_are_reported_once_each():
    text, removed = redact(
        f"key {'sk-ant-api03-' + 'A' * 30} and card 4111111111111111"
    )
    assert removed == ("Anthropic API key", "payment card number")
    assert text.count("removed]") == 2


def test_an_unknown_format_is_withheld_when_the_text_names_it_as_a_credential():
    value = "swordfish-copper-lantern"
    text, removed = redact(f"The credential is {value}")

    assert value not in text
    assert removed == ("password",)


def test_a_quoted_multiword_secret_is_removed_in_full():
    value = "correct horse battery staple"
    text, removed = redact(f'The password is "{value}"')

    assert value not in text
    assert removed == ("password",)


@pytest.mark.parametrize(
    "hostile",
    [
        "sk-ant-api03-\n" + "N" * 30,
        "sk-ant-api03-'" + "N" * 30,
        "ｓｋ－ａｎｔ－ａｐｉ０３－" + "Ｎ" * 30,
        "sk%2Dant%2Dapi03%2D" + "N" * 30,
    ],
)
def test_split_or_unicode_normalized_credentials_fail_closed(hostile):
    text, removed = redact(f"value: {hostile}")

    assert hostile not in text
    assert removed


def test_a_base64_wrapped_credential_is_withheld_as_one_blob():
    secret = "sk-proj-" + "Z" * 40
    encoded = base64.b64encode(secret.encode()).decode()

    text, removed = redact(f"opaque attachment {encoded}")

    assert encoded not in text
    assert secret not in text
    assert removed == ("base64-encoded credential",)


def test_content_larger_than_the_scanner_contract_is_withheld_whole():
    hostile = "x" * MAX_SCAN_CHARS + " credential is never-show-this"

    text, removed = redact(hostile)

    assert "never-show-this" not in text
    assert removed == ("oversized unscanned content",)
