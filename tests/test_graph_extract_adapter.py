"""What the schema-constrained extractor may say when a reply will not parse.

The adapter's prompt is one of the owner's entries, so the reply it gets back
is that entry's text as a model chose to repeat it.  A refusal has to name the
check without repeating the text, and pydantic does the opposite by default.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from varyz.domain import Entry, Kind
from varyz.graph.extract import ModelExtractionEnvelope, SchemaConstrainedExtractor

# Short on purpose: pydantic elides a long ``input_value``, so a long secret
# would make these tests pass whether or not the reply is being repeated.
SECRET = "Prinsengracht 0612345678"


def _entry() -> Entry:
    return Entry(
        id="entry-1",
        kind=Kind.NOTE,
        source="fixture",
        external_id="native-1",
        content_hash="0" * 64,
        body="Anna gave me her address.",
        ingested_at="2026-08-31T12:00:00+00:00",
    )


def _adapter(reply: str) -> SchemaConstrainedExtractor:
    return SchemaConstrainedExtractor(
        lambda system, payload: reply,
        version="fixture-v1",
        prompt_version="fixture-prompt-v1",
        model="no-network-fixture",
    )


def test_the_rendered_validation_error_would_have_repeated_the_reply() -> None:
    """The reason this rule exists, pinned against the version of pydantic in use.

    Without this, the two tests below would pass on a payload pydantic happens
    not to echo, and would go on passing if the adapter started rendering the
    exception again.
    """

    with pytest.raises(ValidationError) as caught:
        ModelExtractionEnvelope.model_validate_json(
            json.dumps({"aliases": [], "claims": SECRET})
        )

    assert SECRET in str(caught.value)


def test_a_reply_that_will_not_parse_is_named_and_not_quoted() -> None:
    """A rendered error carries ``input_value=<the model's reply>``.

    The reply is the archive coming back out of a model, and this message is
    chained into whatever reports the failure -- a run log, a build report, a
    traceback someone pastes into a bug.  What may be repeated is the closed
    vocabulary of pydantic error types.
    """

    adapter = _adapter(json.dumps({"aliases": [], "claims": SECRET}))

    with pytest.raises(ValueError) as caught:
        adapter.extract(_entry())

    message = str(caught.value)
    assert message == "invalid graph extraction schema: list_type"
    assert SECRET not in message


def test_a_reply_that_is_not_json_at_all_is_named_the_same_way() -> None:
    adapter = _adapter(SECRET)

    with pytest.raises(ValueError) as caught:
        adapter.extract(_entry())

    message = str(caught.value)
    assert SECRET not in message
    assert "json_invalid" in message


def test_a_reply_that_parses_still_comes_back_whole() -> None:
    adapter = _adapter('{"aliases": [], "claims": []}')

    batch = adapter.extract(_entry())

    assert batch.aliases == ()
    assert batch.claims == ()
