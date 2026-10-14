"""The renumbered annotation exchange, and what it refuses to restore."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from varyz.personal_model.annotation_wire import (
    MAX_CANDIDATES_PER_EPISODE,
    MAX_SPANS_PER_CANDIDATE,
    WireCandidate,
    WireDialogueContext,
    WireEpisodeAnswer,
    WireGroupResponse,
    WireModality,
    WireSpan,
    category_definitions,
    compact_group_request,
    expand_group_response,
    group_response_model,
    visible_text,
)
from varyz.personal_model.models import (
    AttributionState,
    DialogueContext,
    EntityKind,
    ObservationKind,
    ObservationModality,
    Polarity,
)
from varyz.personal_model.observations import (
    AnnotationBatchRequest,
    AnnotationSource,
    AnnotationSubjectResolution,
    ObservationCandidate,
    _request_dependency_fingerprint,
)


def _restore(candidate: dict) -> ObservationCandidate:
    """Read a candidate the way the compiler does: from the JSON it is sent as.

    The domain models are strict, so a decoded dict is not interchangeable with
    the text it decoded from; only the text validates.
    """

    return ObservationCandidate.model_validate_json(json.dumps(candidate), strict=True)


def _request(
    episode_id: str,
    bodies: tuple[str, ...],
    *,
    speaker: str | None = "person:ada",
    speakers: tuple[str | None, ...] | None = None,
    subjects: tuple[AnnotationSubjectResolution, ...] = (),
) -> AnnotationBatchRequest:
    sources = tuple(
        AnnotationSource(
            entry_id=f"{episode_id}-entry-{index}".ljust(64, "0"),
            content_hash=str(index).ljust(64, "f"),
            body=body,
            source="telegram",
            source_account="owner",
            speaker_source_key=speakers[index] if speakers else speaker,
        )
        for index, body in enumerate(bodies)
    )
    fields = {
        "episode_id": episode_id,
        "target_entry_ids": (sources[0].entry_id,),
        "context_complete": True,
        "sources": sources,
        "allowed_subjects": subjects,
    }
    return AnnotationBatchRequest(
        **fields, dependency_fingerprint=_request_dependency_fingerprint(**fields)
    )


def _candidate(
    *,
    m: int = 1,
    quote: str = "One.",
    spans: tuple[WireSpan, ...] | None = None,
    subject: str | None = "A",
    cost_or_stakes: str | None = None,
) -> WireCandidate:
    return WireCandidate(
        target=1,
        kind=ObservationKind.STATEMENT,
        spans=spans if spans is not None else (WireSpan(m=m, quote=quote),),
        subject=subject,
        dialogue_context=WireDialogueContext.VOLUNTEERED_REASONING,
        modality=WireModality.ASSERTED,
        cost_or_stakes=cost_or_stakes,
        confidence=0.7,
    )


def _answer(n: int, **kwargs) -> WireEpisodeAnswer:
    return WireEpisodeAnswer(n=n, candidates=(_candidate(**kwargs),))


def test_the_sent_payload_carries_no_ids_at_all():
    """Ids are 88% of a request and the model cannot transcribe them anyway."""

    requests = (_request("first", ("I hate mornings.",)), _request("second", ("Fine.",)))

    payload, _ = compact_group_request(requests)
    text = payload.model_dump_json()

    assert "I hate mornings." in text
    for request in requests:
        assert request.episode_id not in text
        for source in request.sources:
            assert source.entry_id not in text
            assert source.content_hash not in text
    # The speaker is a letter, not an opaque account identifier.
    assert "person:ada" not in text


def test_a_reply_in_positions_comes_back_as_a_valid_candidate():
    requests = (_request("first", ("I hate mornings.",)), _request("second", ("Fine.",)))
    payload, legend = compact_group_request(requests)
    assert len(payload.episodes) == 2

    by_episode, unknown, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(2, quote="Fine."),)), legend
    )

    assert not unknown and not refused
    assert set(by_episode) == {"second"}
    restored = _restore(by_episode["second"][0])
    assert restored.episode_id == "second"
    assert restored.target_entry_id == requests[1].sources[0].entry_id
    assert restored.proposition == "Fine."


def test_offsets_are_computed_here_because_the_model_cannot_count():
    """Quoting is evidence; character arithmetic over a payload is not."""

    requests = (
        _request("first", ("Well, I hate mornings.",)),
        _request("second", ("Fine.",)),
    )
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, quote="I hate mornings."),)), legend
    )

    assert not refused
    span = _restore(by_episode["first"][0]).spans[0]
    assert (span.start, span.end) == (6, 22)


def test_an_ordinal_nobody_issued_is_refused_rather_than_resolved():
    requests = (_request("first", ("One.",)), _request("second", ("Two.",)))
    _, legend = compact_group_request(requests)

    by_episode, unknown, _ = expand_group_response(
        WireGroupResponse(responses=(_answer(7),)), legend
    )

    # Seven is not four positions past two; it stands for nothing, and nothing
    # is what it may be attributed to.
    assert unknown == ["7"]
    assert by_episode == {}


def test_a_quotation_from_a_message_that_was_not_sent_is_refused():
    requests = (_request("first", ("One.",)), _request("second", ("Two.",)))
    _, legend = compact_group_request(requests)

    by_episode, unknown, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, m=9),)), legend
    )

    assert not unknown
    assert refused and "message 9" in refused[0]
    # The episode still answers, with the candidate that could not stand dropped.
    assert by_episode == {"first": []}


def test_a_quotation_absent_from_its_message_is_refused_not_given_offsets():
    """Locating a quotation must not manufacture evidence for an invented one.

    Nothing downstream can catch this: offsets no longer come from the model,
    so a fabricated quotation would be given offsets matching its own length
    and would pass every check that follows.
    """

    requests = (_request("first", ("One.",)), _request("second", ("Two.",)))
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, quote="never said this"),)), legend
    )

    assert refused and "not in the message" in refused[0]
    assert by_episode == {"first": []}


def test_the_reply_schema_forbids_reading_a_whole_episode_back_as_evidence():
    """One candidate offered thirty-eight spans around 603 characters of quotation."""

    span = WireSpan(m=1, quote="x")
    with pytest.raises(ValidationError):
        _candidate(spans=tuple(span for _ in range(MAX_SPANS_PER_CANDIDATE + 1)))
    with pytest.raises(ValidationError):
        WireEpisodeAnswer(
            n=1,
            candidates=tuple(
                _candidate() for _ in range(MAX_CANDIDATES_PER_EPISODE + 1)
            ),
        )


def test_the_first_quotation_is_the_proposition():
    """Naming it by number cost a field to say "the first one" 709 times in 827.

    It also refused fifteen sound candidates for naming a quotation past the end
    of their own list, which is arithmetic the model has no business doing.
    """

    requests = (_request("first", ("One. Two.",)), _request("second", ("Two.",)))
    _, legend = compact_group_request(requests)
    answer = WireEpisodeAnswer(
        n=1,
        candidates=(
            _candidate(spans=(WireSpan(m=1, quote="One."), WireSpan(m=1, quote="Two."))),
        ),
    )

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(answer,)), legend
    )

    assert not refused
    restored = _restore(by_episode["first"][0])
    assert restored.proposition == "One."
    assert len(restored.spans) == 2


def test_the_speakers_own_letter_resolves_the_subject():
    """0 of 827 subjects resolved when the model was asked to say so itself.

    A letter cannot contradict itself the way an attribution state, a kind, a
    speaker flag and a name could: it was issued here, so it stands for exactly
    one source key of exactly one kind.
    """

    requests = (_request("first", ("One.",)), _request("second", ("Two.",)))
    payload, legend = compact_group_request(requests)
    letter = payload.episodes[0].messages[0].who
    assert letter is not None

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, subject=letter),)), legend
    )

    assert not refused
    subject = _restore(by_episode["first"][0]).subject
    assert subject.attribution == AttributionState.RESOLVED
    assert subject.source_key == "person:ada"
    # The speaker of an archived message is a person by construction; the model
    # left this null 729 times in 827 when it was asked.
    assert subject.kind == EntityKind.PERSON


def test_a_named_subject_is_offered_by_letter_and_restored_with_its_name():
    subject = AnnotationSubjectResolution(
        source_key="person:ada", kind=EntityKind.PERSON, entity_id="e1", surface="Ada"
    )
    requests = (
        _request("first", ("One.",), subjects=(subject,)),
        _request("second", ("Two.",)),
    )
    payload, legend = compact_group_request(requests)

    assert payload.episodes[0].subjects[0].name == "Ada"
    assert payload.episodes[0].subjects[0].who == payload.episodes[0].messages[0].who

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(
            responses=(_answer(1, subject=payload.episodes[0].subjects[0].who),)
        ),
        legend,
    )

    assert not refused
    restored = _restore(by_episode["first"][0]).subject
    assert restored.source_key == "person:ada"
    # The name comes from the resolver, which knows it, rather than from the
    # model, which would be copying it out of the payload.
    assert restored.surface == "Ada"


def test_no_letter_is_an_unattributed_subject_rather_than_a_refusal():
    """Not knowing who is a valid answer; it just claims nothing."""

    requests = (_request("first", ("One.",)), _request("second", ("Two.",)))
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, subject=None),)), legend
    )

    assert not refused
    subject = _restore(by_episode["first"][0]).subject
    assert subject.attribution == AttributionState.UNKNOWN
    assert subject.source_key is None
    assert subject.refers_to_speaker is False
    assert subject.kind is None


def test_a_speaker_letter_nobody_issued_is_refused():
    requests = (_request("first", ("One.",)), _request("second", ("Two.",)))
    _, legend = compact_group_request(requests)

    _, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, subject="Z"),)), legend
    )

    assert refused and "speaker Z" in refused[0]


def test_a_paraphrased_cost_costs_only_itself():
    requests = (_request("first", ("One.",)), _request("second", ("Two.",)))
    _, legend = compact_group_request(requests)
    answer = WireEpisodeAnswer(
        n=1,
        candidates=(_candidate(cost_or_stakes="a great deal"),),
    )

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(answer,)), legend
    )

    assert not refused
    # Cost or stakes has to be literal source text; paraphrased it would fail
    # the candidate, so it is dropped and the observation stands without it.
    assert _restore(by_episode["first"][0]).cost_or_stakes_literal is None


def test_the_model_is_offered_no_way_to_decline_a_classification():
    """An unknown context or modality is disqualifying everywhere downstream.

    ``InterpretationValidator._qualifies`` rejects both, and
    ``_require_literal_assertion_context`` rejects an unknown modality, so the
    827 candidates that answered ``unknown`` could never become evidence for
    anything. The values are the domain's, less the one that meant nothing.
    """

    assert {member.value for member in WireDialogueContext} == {
        member.value for member in DialogueContext
    } - {DialogueContext.UNKNOWN.value}
    assert {member.value for member in WireModality} == {
        member.value for member in ObservationModality
    } - {ObservationModality.UNKNOWN.value}

    schema = json.dumps(WireCandidate.model_json_schema())
    assert '"unknown"' not in schema


def test_polarity_is_not_asked_for_and_not_guessed():
    """Both readers of polarity use it only to veto.

    A negative observation cannot cut a positive literal fact and cannot become
    a literal memory, so a polarity inferred from the shape of a quotation can
    delete a true memory and can never add one.
    """

    requests = (_request("first", ("I never thought I would like it.",)),)
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(
            responses=(_answer(1, quote="I never thought I would like it."),)
        ),
        legend,
    )

    assert not refused
    assert _restore(by_episode["first"][0]).polarity == Polarity.UNKNOWN


def test_the_reply_shape_pins_how_many_episodes_are_answered():
    """Eighteen calls in 122 answered one episode of the eight they were asked.

    Every one of those replies was within its schema: a list with no length is
    satisfied by one entry, and "answer all eight" was only ever a sentence in
    the prompt. A length makes it a constraint on generation instead.
    """

    pinned = group_response_model(8)
    one = _answer(1)

    with pytest.raises(ValidationError):
        pinned(responses=(one,))
    assert len(pinned(responses=tuple(_answer(n) for n in range(1, 9))).responses) == 8
    # Reading stays forgiving: a short reply is a failure to report, not a
    # failure to parse.
    assert len(WireGroupResponse(responses=(one,)).responses) == 1


def test_every_category_the_model_may_answer_is_defined_where_it_will_read_it():
    """A grammar carries no descriptions, so the schema's are never sent.

    ``llamacpp_local`` sends ``grammar`` and drops ``response_format`` whenever
    the shape converts, which this one does. With the definitions on the fields
    alone, 72 of 72 measured candidates answered ``boundary`` and none of them
    could qualify. The prompt is the only channel left.
    """

    definitions = category_definitions()

    for member in ObservationKind:
        assert member.value in definitions, member
    for member in WireDialogueContext:
        assert member.value in definitions, member
    for member in WireModality:
        assert member.value in definitions, member
    # Named by the code the answer is written in, not by the field's long name.
    assert '"k"' in definitions
    assert '"d"' in definitions
    assert '"o"' in definitions


def test_the_ingests_envelope_is_not_sent_because_the_wire_already_says_it():
    """Two lines of ``From:`` and ``Chat:`` were 39.7% of a chat message's text.

    Both facts are already on the wire — the speaker as a letter, the thread as
    the episode — so the envelope bought nothing and spent the letters' anonymity
    to buy it.
    """

    body = "From: Ada Lovelace\nChat: Analytical Engine\n\nI hate mornings."
    requests = (_request("first", (body,)),)

    payload, _ = compact_group_request(requests)
    text = payload.model_dump_json()

    assert "I hate mornings." in text
    assert "Ada Lovelace" not in text
    assert "Analytical Engine" not in text


def test_a_span_is_located_in_what_was_sent_not_in_the_envelope():
    """The stored body keeps its envelope, so a search from zero would cite it.

    Downstream resolves evidence against the stored body, which means an offset
    found in the envelope would point at characters the model never read.
    """

    body = "From: Ada\nChat: Ada\n\nAda is coming tomorrow."
    _, legend = compact_group_request((_request("first", (body,)),))

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, quote="Ada"),)), legend
    )

    assert not refused
    span = _restore(by_episode["first"][0]).spans[0]
    assert (span.start, span.end) == (21, 24)
    assert body[span.start : span.end] == "Ada"


def test_a_body_that_is_only_an_envelope_is_sent_whole():
    """An empty message on the wire is one the model cannot tell from unsent."""

    assert visible_text("From: Ada\nChat: Ada\n") == ("From: Ada\nChat: Ada\n", 0)
    assert visible_text("From: Ada\nChat: Ada\n\n   \n") == ("From: Ada\nChat: Ada\n\n   \n", 0)


def test_only_the_envelopes_own_lines_go():
    """A forwarding line is about the message, not about how it was addressed."""

    body = "From: Ada\nForwarded from: Charles\n\nHe agrees."
    sent, offset = visible_text(body)

    assert sent == "Forwarded from: Charles\n\nHe agrees."
    assert body[offset:] == sent
    # A body with no envelope at all is untouched, offset and text alike.
    assert visible_text("He agrees.") == ("He agrees.", 0)


# A quotation the model numbers to the wrong message is the single largest
# source of refusals: of 246 distinct refusals measured on 2026-08-30, 153 were
# strings occurring in exactly one message in the whole archive, and only 46
# occurred in none. These four tests fix where the line between the two sits.


def test_a_quotation_numbered_to_the_wrong_message_is_re_addressed():
    """Miscounting the message does not make the quotation less real."""

    said = "I stopped drinking coffee at four."
    requests = (_request("first", ("Let us do it.", said)),)
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, m=1, quote=said),)), legend
    )

    assert not refused
    span = _restore(by_episode["first"][0]).spans[0]
    # Re-addressed to the message that carries it, with offsets into that body.
    assert span.entry_id == "first-entry-1".ljust(64, "0")
    assert (span.start, span.end) == (0, len(said))
    assert span.exact_literal == said
    # A rescue is counted so the run log can show it against the refusals.
    assert legend.relocated == 1


def test_a_call_that_re_addressed_nothing_counts_nothing():
    """The counter reports work done, not opportunities to do it."""

    said = "I stopped drinking coffee at four."
    requests = (_request("first", (said, "Let us do it.")),)
    _, legend = compact_group_request(requests)

    _, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, m=1, quote=said),)), legend
    )

    assert not refused
    assert legend.relocated == 0


def test_a_quotation_in_two_of_the_speakers_messages_stays_refused():
    """Which message was meant is unknowable, so this is not a correction."""

    said = "I stopped drinking coffee at four."
    requests = (_request("first", ("Let us do it.", said, f"{said} Again.")),)
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, m=1, quote=said),)), legend
    )

    assert refused and "not in the message" in refused[0]
    assert by_episode == {"first": []}


def test_a_quotation_only_somebody_else_said_stays_refused():
    """Moving a span across speakers would change who an observation is about.

    That is the one thing re-addressing must never do, so the search never
    leaves the person who was cited, however unique the match.
    """

    said = "I stopped drinking coffee at four."
    requests = (
        _request("first", ("Let us do it.", said), speakers=("person:ada", "person:bo")),
    )
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, m=1, quote=said),)), legend
    )

    assert refused and "not in the message" in refused[0]
    assert by_episode == {"first": []}


def test_a_quotation_too_short_to_be_distinctive_is_not_moved():
    """A two-word agreement is in a hundred messages; finding it proves nothing."""

    requests = (_request("first", ("Let us do it.", "Yes, okay.")),)
    _, legend = compact_group_request(requests)

    by_episode, _, refused = expand_group_response(
        WireGroupResponse(responses=(_answer(1, m=1, quote="Yes, okay."),)), legend
    )

    assert refused and "not in the message" in refused[0]
    assert by_episode == {"first": []}
