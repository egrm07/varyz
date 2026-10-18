"""A compact projection of an annotation exchange, for the model to read and write.

The domain request is addressed by content: every message carries a sixty-four
character entry id and a sixty-four character content hash, and every subject a
full source key.  That is the right way to name things in an archive, and the
wrong way to spend a context window.  Measured on live episodes, 88% of a
request was these names and the field labels around them — 59,000 of 64,238
input tokens — and about half of a reply was the same names copied back, a
character at a time, in place of the quotations they were attached to.

So the exchange is renumbered.  Inside one call each message is given its
position and each speaker a letter, the model reads and answers in those, and
the names are restored here from the mapping that produced them.  Nothing is
inferred in the restoration: an ordinal either was issued for this call or it
was not, and one that was not is refused rather than resolved to a neighbour.

This is a projection, not a contract change.  :class:`AnnotationBatchRequest`
and :class:`ObservationCandidate` are unchanged, still hold the real ids, and
are still what the compiler validates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from varyz.personal_model.models import (
    AttributionState,
    EntityKind,
    ObservationKind,
    Polarity,
)
from varyz.personal_model.observations import (
    OBSERVATION_SCHEMA_VERSION,
    AnnotationBatchRequest,
)

# A single claim needs a handful of quotations to stand up.  One reply offered
# thirty-eight for one candidate — 3,992 characters of spans around 603
# characters of quotation — which is not evidence but the whole episode read
# back. The cap costs nothing a real candidate needs.
MAX_SPANS_PER_CANDIDATE = 6
MAX_CANDIDATES_PER_EPISODE = 8

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class _Wire(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, populate_by_name=True
    )


# --- The reply, written short ------------------------------------------------
#
# Reading costs about a thirtieth of what writing costs per token, so the two
# halves of the exchange are not worth compacting equally.  The request stays in
# words: it is what the model reads to work out what is being asked, and the
# saving there would be small and the risk of obscuring the task real.  The
# reply is where the time goes, and every character of it is generated one
# forward pass at a time — including the field names, which the schema forces
# and the model does not choose.  Those are renamed to single letters, which
# costs nothing but a lookup.
#
# The enum values are a different matter, and they are left in words. Coding
# them to letters was tried, with the legend spelled out in the field's own
# description, and measured against the same episodes answered in words: every
# other field agreed, and `kind` disagreed on seventeen of eighteen shared
# candidates, always by naming the category whose letter was the least
# mnemonic. The model was reading the letters as initials rather than as the
# legend it had been given. Choosing between these values is the judgement
# being asked for, so it is bought back at the price of the words.
# See docs/ANNOTATION_THROUGHPUT.md.
#
# Coding only the two that are nearly constant was tried after that, on the
# reasoning that they cost 15% of every reply -- 24 characters where two would
# do -- while modality was ``asserted`` on 1,841 of 1,874 candidates and context
# was one of two values on 1,840. Each letter was its own category's initial,
# which is how the model wanted to read letters in the first place. It was
# faster and it found more, and it is still refused: on 1,084 episodes answered
# both ways, ``modality`` agreed on 97.6% but ``dialogue_context`` agreed on
# 38.9%, with 311 candidates moving from ``direct_answer`` to
# ``volunteered_reasoning``. A field that changes 58% of its answers has not
# been spelled differently, it has been asked differently.
#
# Modality's own 2.4% is the same fault too small to see: all thirteen
# disagreements ran one way, from ``quoted``, ``uncertain`` and
# ``humorous_or_sarcastic`` to ``asserted``. Coding a field erodes exactly its
# rare values, and in a field that is 98% one value the rare ones are the only
# ones carrying anything. So it is not coded either, and the 3% it was worth is
# left on the table.
#
# What those values may be is a separate matter, and the measurement was
# unambiguous.  Across 827 answered candidates the model said ``unknown`` to
# modality 827 times, to dialogue context 826, and never once said a subject
# was resolved.  Offered a free way to decline a judgement, a 12B model takes
# it every time — and every one of those declines is disqualifying downstream.
# ``InterpretationValidator._qualifies`` rejects an unknown context or modality
# outright, and ``_require_literal_assertion_context`` rejects an unknown
# modality, so not one of those 827 observations could become evidence for an
# interpretation or a literal memory.  Days of reading produced nothing that
# could be used.
#
# So the declines are withdrawn from the reply.  The enums below are the domain
# enums with ``unknown`` removed, and the fields that carry them are required:
# the model must choose, because a refusal to choose was never recorded as a
# refusal, only as a candidate that quietly failed every gate.  What genuinely
# cannot be answered from the text is answered here instead, or not asked at
# all.


class WireDialogueContext(StrEnum):
    """:class:`DialogueContext` without ``unknown``. See the note above."""

    VOLUNTEERED_REASONING = "volunteered_reasoning"
    DIRECT_ANSWER = "direct_answer"
    PROMPTED_AGREEMENT = "prompted_agreement"
    QUOTATION = "quotation"
    BRAINSTORMING = "brainstorming"
    INSTRUCTION = "instruction"
    HUMOUR_OR_SARCASM = "humour_or_sarcasm"
    HYPOTHETICAL = "hypothetical"


class WireModality(StrEnum):
    """:class:`ObservationModality` without ``unknown``. See the note above."""

    ASSERTED = "asserted"
    UNCERTAIN = "uncertain"
    HYPOTHETICAL = "hypothetical"
    QUOTED = "quoted"
    HUMOROUS_OR_SARCASTIC = "humorous_or_sarcastic"


class WireMessage(_Wire):
    """One archived message, numbered within its episode."""

    i: int = Field(ge=1)
    who: str | None = None
    text: str


class WireSubject(_Wire):
    """A subject the resolver has already established, offered by letter."""

    who: str = Field(min_length=1)
    kind: EntityKind
    name: str | None = None


class WireEpisode(_Wire):
    n: int = Field(ge=1)
    # No speakers map: a source key is an opaque identifier the model can do
    # nothing with, and the letters already tell one speaker from another. The
    # ones that have a name have it in ``subjects``.
    messages: tuple[WireMessage, ...]
    targets: tuple[int, ...]
    subjects: tuple[WireSubject, ...] = ()
    partial_context: bool = False


class WireGroupRequest(_Wire):
    episodes: tuple[WireEpisode, ...] = Field(min_length=1)


class WireSpan(_Wire):
    """A quotation, and the number of the message it was taken from."""

    m: int = Field(ge=1, description="the number of the message quoted")
    quote: str = Field(alias="q", min_length=1, description="the words, exactly")


# The categories were sent as bare labels, and a bare label is a guess about
# what a word means to a 12B model rather than an instruction to it.  Withdrawing
# ``unknown`` made it choose; it chose ``brainstorming`` for 124 of 180
# candidates and ``boundary`` for every one of them, which ``_qualifies``
# refuses exactly as it refused ``unknown``.  Forcing a choice only helps if the
# choices are defined, and a definition is read rather than written, so it is
# bought at a thirtieth of what an answer costs.
#
# These are written as field descriptions and also sent in the system prompt,
# because the schema is not sent at all.  Once :func:`gbnf_from_schema` converts
# a shape, ``llamacpp_local`` sends the grammar and drops ``response_format``,
# and a grammar carries no descriptions.  Measured: with the definitions on the
# fields alone, 72 of 72 candidates still said ``boundary``.  The prompt is the
# only channel the model reads.
_KIND = (
    "what the message does: statement, says how something is; reason, gives why; "
    "choice, settles on one option over another; tradeoff, weighs what is given "
    "up against what is gained; boundary, states a limit on what the speaker "
    "will do; commitment, undertakes to do something; reaction, responds to "
    "something that happened"
)
_CONTEXT = (
    "how the words came to be said: volunteered_reasoning, the speaker offers "
    "their own reason unasked; direct_answer, they answer a question put to "
    "them; prompted_agreement, they agree with what someone else proposed; "
    "quotation, they repeat another's words; brainstorming, they float an option "
    "they have not settled on; instruction, they tell someone what to do; "
    "humour_or_sarcasm, not meant literally; hypothetical, about a situation "
    "that is not the case"
)
_MODALITY = (
    "how it is put: asserted, stated as being so; uncertain, hedged or in doubt; "
    "hypothetical, conditional on something that is not the case; quoted, "
    "attributed to someone other than the speaker; humorous_or_sarcastic, not "
    "meant literally"
)


def category_definitions() -> str:
    """What the three coded fields mean, for the prompt that has to carry them."""

    return (
        f'For "k", {_KIND}. '
        f'For "d", {_CONTEXT}. '
        f'For "o", {_MODALITY}. '
        "Judge what was actually said: most messages state or explain something "
        'rather than set a limit, and "hypothetical" belongs only to words about '
        "a situation that is not the case."
    )


class WireCandidate(_Wire):
    target: int = Field(alias="t", ge=1, description="the message being annotated")
    kind: ObservationKind = Field(alias="k", description=_KIND)
    # The first quotation states the observation. It used to be named by number
    # in a field of its own, which cost a field on every candidate to say "the
    # first one" 709 times out of 827, and refused fifteen otherwise sound
    # candidates for naming a quotation past the end of their own list.
    spans: tuple[WireSpan, ...] = Field(
        alias="e",
        min_length=1,
        max_length=MAX_SPANS_PER_CANDIDATE,
        description="quotations, the first of which states the observation",
    )
    # One letter, where there used to be an attribution state, a kind, a letter,
    # a speaker flag and a surface name — four of which the model left empty
    # every time, and which together could contradict each other into a subject
    # that no repair could resolve.  The letter is enough: every speaker has one
    # and so does every subject the resolver established, and what kind of thing
    # a letter stands for is already known here without asking.
    subject: str | None = Field(
        alias="b",
        description=(
            "the letter of whoever the observation is about, which is normally "
            "the letter of whoever wrote the message; null if it is unclear"
        ),
    )
    dialogue_context: WireDialogueContext = Field(alias="d", description=_CONTEXT)
    modality: WireModality = Field(alias="o", description=_MODALITY)
    # Required, and written as null when there is none. An optional field means
    # every subset of the object is a legal reply, which is a grammar this
    # cannot write and a licence the model does not need.
    cost_or_stakes: str | None = Field(
        alias="s", description="source words naming what it costs, or null"
    )
    confidence: float = Field(alias="c", ge=0, le=1)


class WireEpisodeAnswer(_Wire):
    n: int = Field(ge=1, description="the number of the episode being answered")
    candidates: tuple[WireCandidate, ...] = Field(
        alias="c", max_length=MAX_CANDIDATES_PER_EPISODE
    )


class WireGroupResponse(_Wire):
    responses: tuple[WireEpisodeAnswer, ...] = Field(default=(), alias="r")


_GROUP_RESPONSE_MODELS: dict[int, type[WireGroupResponse]] = {}


def group_response_model(count: int) -> type[WireGroupResponse]:
    """The reply shape for a call about exactly ``count`` episodes.

    Eighteen of 122 calls answered about one episode when they had been asked
    about eight, losing 126 episodes that were then read again from scratch.
    The reply was within its schema every time: the list of answers had no
    length, so one answer satisfied it, and the instruction to return eight was
    only ever a sentence in the prompt.  Given a length the grammar cannot
    finish the reply early, which is a constraint on generation rather than a
    request of it.
    """

    cached = _GROUP_RESPONSE_MODELS.get(count)
    if cached is not None:
        return cached
    model = create_model(
        "WireGroupResponse",
        __base__=WireGroupResponse,
        responses=(
            tuple[WireEpisodeAnswer, ...],
            Field(alias="r", min_length=count, max_length=count),
        ),
    )
    _GROUP_RESPONSE_MODELS[count] = model
    return model


class _LetterBook:
    """Short names for the speakers of one episode, issued on first sight."""

    __slots__ = ("by_letter", "by_source_key")

    def __init__(self) -> None:
        self.by_source_key: dict[str, str] = {}
        self.by_letter: dict[str, str] = {}

    def letter_for(self, source_key: str | None) -> str | None:
        if not source_key:
            return None
        existing = self.by_source_key.get(source_key)
        if existing is not None:
            return existing
        index = len(self.by_source_key)
        # Past the alphabet the letters double, which stays short and stays
        # unique; an episode with that many speakers is vanishingly rare.
        letter = (
            _ALPHABET[index]
            if index < len(_ALPHABET)
            else _ALPHABET[index // len(_ALPHABET) - 1] + _ALPHABET[index % len(_ALPHABET)]
        )
        self.by_source_key[source_key] = letter
        self.by_letter[letter] = source_key
        return letter


# Nine tenths of the archive's bodies open with an envelope the ingest wrote:
# ``From: <speaker>`` and ``Chat: <thread>``, then a blank line.  Both facts are
# already on the wire — the speaker as ``who``, the thread as the episode that
# holds the message — so sending the envelope spends about half of every
# message's characters repeating what the model has already been told, in the
# one form that also undoes the letters' anonymity.  Measured over 20,000
# bodies: 32.2 envelope characters against 65.3 in the whole body.
#
# Only the envelope's own lines go.  A ``Forwarded from`` line says something
# about the message rather than about the wire, and stays.
_ENVELOPE = ("From:", "Chat:", "Conversation:")


def visible_text(body: str) -> tuple[str, int]:
    """The message as the model should read it, and where it starts in ``body``.

    The offset matters as much as the text.  Spans are located by searching the
    stored body for the quotation, and the stored body still has its envelope;
    without somewhere to start, a quotation of a name would be found in the
    envelope that was never sent and the span would cite characters nobody read.

    A body that is nothing but its envelope is returned whole.  There is no
    reading of it that helps, and an empty message on the wire is a message the
    model cannot tell from one that was never sent.
    """

    offset = 0
    for line in body.splitlines(keepends=True):
        if not line.startswith(_ENVELOPE):
            break
        offset += len(line)
    if not offset:
        return body, 0
    while offset < len(body) and body[offset] == "\n":
        offset += 1
    remainder = body[offset:]
    if not remainder.strip():
        return body, 0
    return remainder, offset


class GroupLegend:
    """What each number and letter in one call stands for."""

    __slots__ = ("_by_episode", "_order")

    def __init__(self, requests: Sequence[AnnotationBatchRequest]) -> None:
        self._order = tuple(request.episode_id for request in requests)
        self._by_episode: dict[str, _EpisodeLegend] = {}

    def register(self, episode_id: str, legend: _EpisodeLegend) -> None:
        self._by_episode[episode_id] = legend

    def episode_id(self, ordinal: int) -> str | None:
        if 1 <= ordinal <= len(self._order):
            return self._order[ordinal - 1]
        return None

    def episode(self, episode_id: str) -> _EpisodeLegend | None:
        return self._by_episode.get(episode_id)

    @property
    def relocated(self) -> int:
        """Quotations this call re-addressed, over every episode in it.

        A refusal is visible in the log; a rescue is not, and the two move
        together.  Counting both is what makes the trade legible: rescues that
        never rise while refusals fall would mean the model simply stopped
        quoting, which is not the improvement it would look like.
        """

        return sum(episode.relocated for episode in self._by_episode.values())

    def __len__(self) -> int:
        return len(self._order)


# A quotation short enough to recur by accident is not distinctive enough to
# re-address on the strength of being found. "ok" and "yes exactly" occur in
# hundreds of messages; of the refused quotations measured on 2026-08-30, the
# ones that occurred in exactly one message had a median length of 35.
#
# Lowering it to 8 was measured on 2026-08-31 and rejected. Of the 1,667
# distinct refusals across this run set, 201 fall in the 8-11 band and 92 of
# them (45.8%) occur in exactly one message of the archive -- close enough to
# the 20-39 band this already admits (56.1%) to look like a fair trade. It is
# not. 132 of the 201 are one or two plain words, which is the shape the
# threshold exists to exclude, and only 42.4% of those are unique. What
# lowering it would really buy is the 21 that are distinctive in spite of being
# short, because they carry a digit or a token's punctuation -- a version
# number, a flag, a filename -- and 21 strings do not pay for admitting 132
# conversational fragments. Below 8 the case collapses outright: the 4-7 band
# is 20.4% unique and 0-3 is 2.8%.
#
# Two things that measurement is not. Archive-wide uniqueness is a proxy for a
# rule that searches only one episode, so it understates how many would be
# found and overstates how ambiguous they are. And it says nothing about the
# next run either way: annotation is 99.88% cached, so this path decides
# nothing until an ingest brings new episodes.
_RELOCATABLE_QUOTE_CHARS = 12


class _EpisodeLegend:
    __slots__ = (
        "bodies",
        "entry_by_ordinal",
        "relocated",
        "source_key_by_letter",
        "speaker_by_entry",
        "subject_by_letter",
        "visible_from",
    )

    def __init__(
        self,
        entry_by_ordinal: Mapping[int, str],
        source_key_by_letter: Mapping[str, str],
        bodies: Mapping[str, str],
        subject_by_letter: Mapping[str, tuple[EntityKind, str | None]],
        visible_from: Mapping[str, int] | None = None,
        speaker_by_entry: Mapping[str, str] | None = None,
    ) -> None:
        self.entry_by_ordinal = dict(entry_by_ordinal)
        self.source_key_by_letter = dict(source_key_by_letter)
        self.bodies = dict(bodies)
        # Where each body's sent text begins, so a span is looked for only in
        # what was sent. Bodies are kept whole because the offsets they carry
        # are what every reader downstream resolves evidence against.
        self.visible_from = dict(visible_from or {})
        # What each letter stands for: a kind, and a name where one is known.
        # Asked of the model these were null 729 times in 827; known here for
        # every letter that was issued.
        self.subject_by_letter = dict(subject_by_letter)
        # Who sent each message, so a misnumbered quotation can only ever be
        # moved to another message by the same person.
        self.speaker_by_entry = dict(speaker_by_entry or {})
        self.relocated = 0

    def relocate(self, quote: str, cited_entry_id: str) -> tuple[str, int] | None:
        """Find the message that really contains a misnumbered quotation.

        The model quotes the text accurately and labels it with the wrong
        message far more often than it invents anything: of 352 distinct
        refusals measured on 2026-08-30, 209 were strings that occur in exactly
        one message in the whole archive, 66 in several, and only 77 in none.
        Refusing the first group threw away real, verbatim evidence over an
        index.

        Folding punctuation before the search was measured on the same 352 and
        rejected: smart quotes, dashes and non-breaking spaces account for 5 of
        them, 1.4%, which does not pay for loosening what "found verbatim"
        means in the one rule the evidence rests on.

        Correcting the index is not the same as relaxing the evidence rule, and
        this deliberately stays narrow enough to keep that true. The quotation
        must still be found verbatim, inside the sent region of the message that
        ends up cited. It is moved only to a message *the same person sent*, so
        nothing an attribution depends on can change. And it is moved only when
        exactly one such message contains it, so an ambiguous match is refused
        exactly as before.
        """
        if len(quote) < _RELOCATABLE_QUOTE_CHARS:
            return None
        speaker = self.speaker_by_entry.get(cited_entry_id)
        if speaker is None:
            return None
        found: tuple[str, int] | None = None
        for entry_id, body in self.bodies.items():
            if entry_id == cited_entry_id:
                continue
            if self.speaker_by_entry.get(entry_id) != speaker:
                continue
            at = body.find(quote, self.visible_from.get(entry_id, 0))
            if at < 0:
                continue
            if found is not None:
                # Two messages by the same person carry it. Which one the
                # candidate meant is unknowable, so this stays a refusal.
                return None
            found = (entry_id, at)
        if found is not None:
            self.relocated += 1
        return found


def compact_group_request(
    requests: Sequence[AnnotationBatchRequest],
) -> tuple[WireGroupRequest, GroupLegend]:
    """Renumber a group of requests, returning the payload and its legend."""

    legend = GroupLegend(requests)
    episodes: list[WireEpisode] = []
    for position, request in enumerate(requests, 1):
        entry_by_ordinal: dict[int, str] = {}
        ordinal_by_entry: dict[str, int] = {}
        letters = _LetterBook()
        bodies: dict[str, str] = {}
        visible_from: dict[str, int] = {}
        speaker_by_entry: dict[str, str] = {}

        messages: list[WireMessage] = []
        for ordinal, source in enumerate(request.sources, 1):
            entry_by_ordinal[ordinal] = source.entry_id
            ordinal_by_entry[source.entry_id] = ordinal
            sent, offset = visible_text(source.body)
            bodies[source.entry_id] = source.body
            visible_from[source.entry_id] = offset
            if source.speaker_source_key is not None:
                speaker_by_entry[source.entry_id] = source.speaker_source_key
            messages.append(
                WireMessage(
                    i=ordinal,
                    who=letters.letter_for(source.speaker_source_key),
                    text=sent,
                )
            )
        subjects = tuple(
            WireSubject(who=letter, kind=subject.kind, name=subject.surface)
            for subject in request.allowed_subjects
            if (letter := letters.letter_for(subject.source_key)) is not None
        )
        # A speaker of an archived message is a person by construction, so every
        # letter has a kind before the resolver adds anything. The resolver's
        # subjects are applied second because they also carry a name.
        subject_by_letter: dict[str, tuple[EntityKind, str | None]] = {
            letter: (EntityKind.PERSON, None) for letter in letters.by_letter
        }
        for subject in subjects:
            subject_by_letter[subject.who] = (subject.kind, subject.name)
        legend.register(
            request.episode_id,
            _EpisodeLegend(
                entry_by_ordinal,
                letters.by_letter,
                bodies,
                subject_by_letter,
                visible_from,
                speaker_by_entry,
            ),
        )
        episodes.append(
            WireEpisode(
                n=position,
                messages=tuple(messages),
                targets=tuple(
                    ordinal_by_entry[entry_id]
                    for entry_id in request.target_entry_ids
                    if entry_id in ordinal_by_entry
                ),
                subjects=subjects,
                partial_context=not request.context_complete,
            )
        )
    return WireGroupRequest(episodes=tuple(episodes)), legend


class ExpansionFailure(ValueError):
    """A reply that cannot be restored to real ids without inventing one."""


_UNATTRIBUTED = {
    "attribution": AttributionState.UNKNOWN.value,
    "kind": None,
    "source_key": None,
    "refers_to_speaker": False,
    "surface": None,
}


def _expand_subject(letter: str | None, episode: _EpisodeLegend) -> dict[str, Any]:
    """Turn the one letter the model answered into a subject, or withdraw.

    :class:`SubjectCandidate` requires that a resolved subject name exactly one
    reference and a kind, and that an unresolved one name none.  Asked for those
    separately the model contradicted itself constantly and resolved nothing at
    all — 0 of 827.  Asked for a letter, it cannot contradict itself: the letter
    was issued by this module, so it either stands for exactly one source key
    with exactly one kind or it was never issued, and one that was never issued
    is refused rather than resolved to a neighbour.
    """

    if letter is None:
        return dict(_UNATTRIBUTED)
    source_key = episode.source_key_by_letter.get(letter)
    known = episode.subject_by_letter.get(letter)
    if source_key is None or known is None:
        raise ExpansionFailure(f"subject names speaker {letter}, not sent")
    kind, surface = known
    return {
        "attribution": AttributionState.RESOLVED.value,
        "kind": kind.value,
        "source_key": source_key,
        # The letter is the reference, and it is the only one; a separate
        # "it was the speaker" flag could only ever contradict it.
        "refers_to_speaker": False,
        # An omitted string arrives as an empty one under a schema that cannot
        # express "absent"; downstream both mean the same thing and only one of
        # them validates.
        "surface": surface or None,
    }


def expand_candidate(
    candidate: WireCandidate, episode_id: str, episode: _EpisodeLegend
) -> dict[str, Any]:
    """Restore one candidate's real ids, or refuse it."""

    spans: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for span in candidate.spans:
        entry_id = episode.entry_by_ordinal.get(span.m)
        if entry_id is None:
            raise ExpansionFailure(f"span cites message {span.m}, which was not sent")
        body = episode.bodies.get(entry_id, "")
        start = body.find(span.quote, episode.visible_from.get(entry_id, 0))
        if start < 0:
            # Miscounted the message, or invented the quotation?  Ask the
            # episode: if one other message by the same person carries this text
            # verbatim, the number was wrong and the evidence was not.
            elsewhere = episode.relocate(span.quote, entry_id)
            if elsewhere is None:
                # Refused here, because nowhere downstream can catch it.  The
                # model no longer sends offsets; they are computed from where the
                # quotation is found.  A quotation that is nowhere in the message
                # it names would be given offsets consistent with its own length
                # and pass every later check as genuine evidence.
                raise ExpansionFailure(
                    f"quotation {span.quote[:40]!r} is not in the message it cites"
                )
            entry_id, start = elsewhere
        located = (entry_id, start, start + len(span.quote))
        if located in seen:
            # The same quotation offered twice is one piece of evidence, and
            # duplicate spans are rejected outright downstream.
            continue
        seen.add(located)
        spans.append(
            {
                "entry_id": entry_id,
                "start": start,
                "end": start + len(span.quote),
                "exact_literal": span.quote,
            }
        )
    target_entry_id = episode.entry_by_ordinal.get(candidate.target)
    if target_entry_id is None:
        raise ExpansionFailure(f"candidate targets message {candidate.target}, not sent")

    return {
        "episode_id": episode_id,
        "target_entry_id": target_entry_id,
        "kind": candidate.kind.value,
        "proposition": spans[0]["exact_literal"],
        "spans": spans,
        "subject": _expand_subject(candidate.subject, episode),
        "dialogue_context": candidate.dialogue_context.value,
        # Not asked, and not derived either.  Both readers of polarity use it
        # only to veto — a negative observation cannot cut a positive literal
        # fact, and cannot become a literal memory — so a value guessed from
        # the shape of a quotation can delete a true memory and can never add
        # one.  Negation scope over a fragment is exactly where that guess is
        # least reliable: "I never thought I'd like it" negates nothing the
        # observation asserts.  Unknown is what is actually known.
        "polarity": Polarity.UNKNOWN.value,
        "modality": candidate.modality.value,
        # Cost or stakes must be literal text from the evidence. Paraphrased,
        # it fails the whole candidate; dropped, it costs only itself.
        "cost_or_stakes_literal": (
            candidate.cost_or_stakes
            if candidate.cost_or_stakes
            and any(candidate.cost_or_stakes in span["exact_literal"] for span in spans)
            else None
        ),
        "confidence": candidate.confidence,
    }


def expand_group_response(
    parsed: WireGroupResponse, legend: GroupLegend
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[str]]:
    """Restore a whole reply.

    Returns the candidates by episode id, the ordinals that named no episode,
    and the reasons individual candidates were refused.  Both are reported
    rather than absorbed: a reply that addresses episodes nobody asked about is
    a different failure from one that answers correctly about fewer of them.
    """

    by_episode: dict[str, list[dict[str, Any]]] = {}
    unknown: list[str] = []
    refused: list[str] = []
    for answer in parsed.responses:
        episode_id = legend.episode_id(answer.n)
        episode = legend.episode(episode_id) if episode_id is not None else None
        if episode_id is None or episode is None:
            unknown.append(str(answer.n))
            continue
        if episode_id in by_episode:
            continue
        candidates: list[dict[str, Any]] = []
        for candidate in answer.candidates:
            try:
                candidates.append(expand_candidate(candidate, episode_id, episode))
            except ExpansionFailure as exc:
                refused.append(str(exc))
        by_episode[episode_id] = candidates
    return by_episode, unknown, refused


def episode_payload(episode_id: str, candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "episode_id": episode_id,
        "candidates": list(candidates),
    }
