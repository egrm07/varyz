"""Versioned extraction adapters and resumable graph build orchestration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import getaddresses

from pydantic import BaseModel, ConfigDict, ValidationError

from varyz.domain import Entry
from varyz.evidence import EvidenceReader
from varyz.graph.cache import ExtractionCache
from varyz.graph.languages import (
    CLAUSE_TAIL_RE,
    CONTEXT_DEPENDENT_BELIEF_RE,
    NAME_SAID_MOVE_RE,
    TRAILING_TIME_RE,
    match_claim,
    modality_of,
    polarity_of,
)
from varyz.graph.models import (
    ClaimCandidate,
    ClaimModality,
    EvidenceCandidate,
    ExtractionBatch,
    ObjectType,
    PlanState,
)
from varyz.graph.repository import GraphRepository, RebuildResult
from varyz.graph.temporal import parse_time_interval
from varyz.llm.refusal import refusal_reason

HEADER_RE = re.compile(r"^([A-Za-z-]+):\s*(.*)$")
SENTENCE_RE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")


class ExtractionUnavailable(RuntimeError):
    """The extractor stopped answering, so the run stops asking.

    One entry failing is a hole in a generation. Every entry failing is a dead
    model, and staging 45,000 empty batches over it would produce a generation
    that looks built and says nothing.
    """


def _extraction_failure(error: Exception) -> ExtractionBatch:
    """A batch that records the hole rather than pretending there is none.

    Only the exception's type is kept. A schema-validation message quotes the
    model's output back, and the model's output is derived from archive text —
    that does not belong in a quarantine row.
    """
    return ExtractionBatch(
        quarantined=(
            {
                "predicate": "extraction_failed",
                "reason_code": "extraction_failed",
                "error_type": type(error).__name__,
            },
        )
    )


def _headers(body: str) -> tuple[dict[str, str], str, int]:
    head, separator, rest = body.partition("\n\n")
    if not separator:
        return {}, body, 0
    parsed = {}
    for line in head.splitlines():
        match = HEADER_RE.match(line)
        if match:
            parsed[match.group(1).casefold()] = match.group(2).strip()
    if not parsed:
        return {}, body, 0
    return parsed, rest, len(head) + len(separator)


# Somebody is being spoken to. A message can name the owner while being from
# them ("tell Egor I said hi"), so the second person is what separates a letter
# written to him from a mention of him.
_SECOND_PERSON_RE = re.compile(
    r"\b(?:you|your|yours|you're|youre|u)\b"
    r"|\b(?:ты|тебя|тебе|тобой|твой|твоя|твои|вы|вас|вам|ваш\w*)\b"
    r"|\b(?:t(?:ú|u)|te|ti|tuyo|tuya|usted|vosotros)\b",
    re.IGNORECASE,
)

# A name used to call somebody, not to talk about them. It carries a greeting
# in front or a comma behind — "Dear Egor", "Egor, you're…", "…thank you for
# everything Egor" — and that punctuation is the whole difference from
# "would Egor really want that?", which he wrote about himself.
_GREETING = (
    r"(?:hi|hey|hello|dear|yo|привет|здравствуй\w*|дорог\w+|hola|querid\w+|buenas)"
)
_GREETED_RE = re.compile(rf"{_GREETING}[\s,]+$", re.IGNORECASE)
_VOCATIVE_TAIL_RE = re.compile(r"\s*[,!?…]|\s*$|\s*\n")

# The vocative belongs to a letter he is quoting, not to this message: "she
# said: Dear Egor, the tenancy requires you to…" is him reporting what he
# was told, and everything he says around it is still his.
_QUOTE_INTRO_RE = re.compile(
    r"(?:said|says|wrote|writes|replied|asked|сказал\w*|говорит|пишет|написал\w*)"
    rf"\s*[:\-–—]\s*[\"«“']?\s*(?:{_GREETING}\s+)?$",
    re.IGNORECASE,
)


def addresses_owner(body: str, owner_names: Sequence[str]) -> bool:
    """Whether this message is written *to* the archive's owner.

    Calling the owner by name while speaking in the second person is somebody
    writing to him. He would do neither about himself, so a message shaped this
    way was not his however the connector filed it — which is the only handle
    there is on an entry whose `From:` header says `me` and whose body says
    "You always know what to say, Egor".

    It has to be the name used as a *call*, though. Measured over the live
    archive, accepting any mention of him plus a "you" anywhere refused 70
    messages and took 7 claims with it, only 3 of which were somebody else's —
    his own planning note lost three real goals because it asked "would Egor
    actually build this?" halfway down. Requiring the greeting or the comma
    leaves 6 messages and 3 claims, and all three are another person's words.
    """
    if not owner_names or not body:
        return False
    if not _SECOND_PERSON_RE.search(body):
        return False
    for name in owner_names:
        stripped = name.strip()
        if not stripped:
            continue
        for match in re.finditer(rf"\b{re.escape(stripped)}\b", body, re.IGNORECASE):
            before = body[max(0, match.start() - 32) : match.start()]
            if _QUOTE_INTRO_RE.search(before):
                continue
            if _GREETED_RE.search(before) or _VOCATIVE_TAIL_RE.match(body, match.end()):
                return True
    return False


def _is_one_to_one(entry: Entry) -> bool:
    """Whether this conversation holds exactly one other person.

    Each source says so in its own spelling, and both are the transport's own
    encoding rather than anything Varyz guesses. WhatsApp gives a group the
    `@g.us` suffix. Telegram numbers a user positive and a group or channel
    negative — the export prints everything positive, and `canonical_chat_id`
    restates it the way MTProto does precisely so this holds for both
    transports.

    Unknown shapes answer False. Being wrong here merges two people into one,
    which is the failure that cannot be seen from the outside; splitting one
    person into two is visible in `varyz graph review` and mergeable.
    """
    thread = (entry.thread_id or "").strip()
    if not thread:
        return False
    if entry.source == "whatsapp":
        return not thread.endswith("@g.us")
    if entry.source == "telegram":
        return not thread.removeprefix("tg:").lstrip().startswith("-")
    return False


# Every shape `_sender_alias` can return, and nothing else in a batch: the other
# aliases an extraction makes are keyed `name:`, `place:` or `project:`, all
# scoped to the entry. This is how a cached batch's speaker is found again
# without storing which alias that was.
_SENDER_KEY_PREFIXES = ("email:", "whatsapp:", "telegram:", "display:")


def _rekey_claim(claim: ClaimCandidate, stale: set[str], key: str) -> ClaimCandidate:
    """The same claim, said by whoever the current identity rule says said it."""
    if not stale:
        return claim
    update: dict[str, object] = {}
    if claim.subject_key in stale:
        update["subject_key"] = key
    if claim.asserted_by_key in stale:
        update["asserted_by_key"] = key
    if any(item.speaker_key in stale for item in claim.evidence):
        update["evidence"] = tuple(
            item.model_copy(update={"speaker_key": key})
            if item.speaker_key in stale
            else item
            for item in claim.evidence
        )
    return claim.model_copy(update=update) if update else claim


def owner_version_suffix(owner_names: Sequence[str]) -> str:
    """What to append to an extractor's version once it knows the owner's name.

    Knowing the name changes which messages read as the owner's own words, so a
    generation built with it is a different reading of the same corpus. Both the
    generation key and the extraction cache scope are built from the extractor
    version and nothing else about it, so without this, filling in
    `[owner] names` and running `varyz graph rebuild` is a no-op: the run is
    recognised as one already done and the misattributed claims stay.

    Empty names return an empty suffix, so an archive that has never named its
    owner keeps the version — and the cached extractions — it already has.
    """
    names = sorted({name.strip().casefold() for name in owner_names if name.strip()})
    if not names:
        return ""
    digest = hashlib.sha256("\0".join(names).encode("utf-8")).hexdigest()[:12]
    return f"+owner.{digest}"


def _is_me(
    value: str,
    source_account: str,
    *,
    authored_by_user: bool = False,
    body: str = "",
    owner_names: Sequence[str] = (),
) -> bool:
    """Whether the speaker of an entry is the archive's owner.

    The stored flag is asked first, because it is the only answer that does not
    depend on how a connector chose to spell the owner's name. WhatsApp writes
    "From: me" and Telegram writes the display name, so name-matching alone
    filed 3,849 of the owner's own Telegram messages under a stranger and kept
    every one of them out of their Mirror.

    The flag is not infallible, though, and `addresses_owner` is the one thing
    that can overrule it: a message that calls the owner by name while speaking
    to him was written by somebody else, whatever the header claims.
    """
    if authored_by_user:
        return not addresses_owner(body, owner_names)
    lowered = value.casefold().strip()
    account = source_account.casefold().strip()
    return lowered == "me" or bool(account and account in lowered)


def _clean_object(value: str) -> str:
    value = TRAILING_TIME_RE.split(value, maxsplit=1)[0]
    value = CLAUSE_TAIL_RE.split(value, maxsplit=1)[0]
    return value.strip(" \t\r\n,;:-")


def _alias(
    key: str,
    entity_type: str,
    label: str,
    namespace: str,
    value: str,
    entry_id: str,
    *,
    verified: bool,
    confidence: float = 1.0,
) -> dict:
    return {
        "key": key,
        "entity_type": entity_type,
        "label": label,
        "namespace": namespace,
        "value": value,
        "source_entry_id": entry_id,
        "verification_state": "verified" if verified else "candidate",
        "confidence": confidence,
    }


class DeterministicExtractor:
    name = "deterministic"
    # Reads one entry and nothing around it, so a cached answer stays valid
    # however its neighbours change.
    reads_context = False
    # 3: reads Russian and Spanish as well as English. A version the graph can
    # see, because a generation built by the English-only patterns and one built
    # by these are different readings of the same archive.
    # 4: a claim's object stops at the clause after it, so a place is a place
    # and not the remainder of the sentence.
    version = "4"
    prompt_version = None
    model = None

    def __init__(self, owner_names: Sequence[str] = ()):
        self.owner_names = tuple(owner_names)
        self.version = type(self).version + owner_version_suffix(self.owner_names)

    def identify(self, entry: Entry) -> tuple[str, dict | None]:
        """Who is speaking in this entry, and the alias to file them under.

        Separated from `extract` because it is the one part of an extraction
        that costs nothing and must never be remembered — see `reidentify`.
        """
        headers, text, _ = _headers(entry.body)
        sender = headers.get("from", "")
        if not sender or _is_me(
            sender,
            entry.source_account,
            authored_by_user=entry.authored_by_user,
            body=text,
            owner_names=self.owner_names,
        ):
            return "self", None
        return self._sender_alias(entry, sender)

    def reidentify(self, entry: Entry, batch: ExtractionBatch) -> ExtractionBatch:
        """Re-answer *who spoke* over an answer that was read back from cache.

        A cached batch is kept because asking the model again is expensive.
        Working out whose message it was is not expensive, and no model was ever
        asked — so remembering it is pure cost: an identity rule corrected today
        would otherwise reach only the entries that happen to be re-read, and
        landing it everywhere would mean re-extracting the whole archive through
        the model to recover something a regex already knows. That is exactly
        what happened when group chats stopped being read as people: the fix was
        correct, the rebuild reused 45,528 cached answers, and all 84
        misattributed claims came back unchanged.

        Claims and their evidence carry the speaker by key, so the keys move
        with the alias or the batch would name somebody nothing points at.
        """
        new_key, new_alias = self.identify(entry)
        stale = {
            str(alias["key"])
            for alias in batch.aliases
            if str(alias.get("key", "")).startswith(_SENDER_KEY_PREFIXES)
        }
        if stale == ({new_key} if new_alias is not None else set()):
            return batch
        aliases = tuple(
            alias for alias in batch.aliases if str(alias["key"]) not in stale
        ) + ((new_alias,) if new_alias is not None else ())
        return batch.model_copy(
            update={
                "aliases": aliases,
                "claims": tuple(
                    _rekey_claim(claim, stale, new_key) for claim in batch.claims
                ),
            }
        )

    def extract(self, entry: Entry) -> ExtractionBatch:
        headers, text, body_offset = _headers(entry.body)
        aliases: list[dict] = [
            _alias(
                "self",
                "person",
                "Me",
                "system",
                "self",
                entry.id,
                verified=True,
            )
        ]
        sender_key, sender_alias = self.identify(entry)
        if sender_alias is not None:
            aliases.append(sender_alias)

        observation_text = entry.captured_at or entry.occurred_start
        observed = (
            datetime.fromisoformat(observation_text) if observation_text else None
        )
        if observed is not None:
            observed = (
                observed if observed.tzinfo else observed.replace(tzinfo=UTC)
            ).astimezone(UTC)
        claims: list[ClaimCandidate] = []
        for sentence_match in SENTENCE_RE.finditer(text):
            sentence = sentence_match.group(0).strip()
            if not sentence:
                continue
            start = body_offset + sentence_match.start() + (
                len(sentence_match.group(0)) - len(sentence_match.group(0).lstrip())
            )
            end = start + len(sentence)
            modality = modality_of(sentence)
            polarity = polarity_of(sentence)
            time = parse_time_interval(sentence, observed)

            report = NAME_SAID_MOVE_RE.search(sentence)
            if report:
                name = report.group("name")
                subject_key = f"name:{name.casefold()}:{entry.id}"
                aliases.append(
                    _alias(
                        subject_key,
                        "person",
                        name,
                        "display_name",
                        name,
                        entry.id,
                        verified=False,
                        confidence=0.7,
                    )
                )
                place = _clean_object(report.group("object"))
                place_key = self._place_alias(aliases, place, entry.id)
                claims.append(
                    self._claim(
                        entry,
                        sentence,
                        start,
                        end,
                        subject_key,
                        "moved_to",
                        ObjectType.ENTITY,
                        place,
                        place_key,
                        ClaimModality.REPORT,
                        sender_key,
                        observed,
                        time,
                        polarity,
                    )
                )
                continue

            found = match_claim(sentence)
            if found is None:
                continue
            matched, match = found
            predicate = matched.predicate
            object_type = matched.object_type
            surface = _clean_object(match.group("object"))
            if not surface:
                continue
            # "I think so", «я думаю, что да», "creo que sí" affirm prior
            # conversational context. Without resolving that antecedent, storing
            # "so" as the belief object invents a useless standalone claim.
            if predicate == "believed" and CONTEXT_DEPENDENT_BELIEF_RE.match(surface):
                continue
            object_key = None
            if object_type == ObjectType.ENTITY:
                if predicate in {"moved_to", "lived_in"}:
                    object_key = self._place_alias(aliases, surface, entry.id)
                else:
                    object_key = self._project_alias(aliases, surface, entry.id)
            claim_modality = modality
            if predicate == "planned_to":
                claim_modality = ClaimModality.PLAN
            elif predicate == "promised_to":
                claim_modality = ClaimModality.PROMISE
            elif predicate == "requested":
                claim_modality = ClaimModality.REQUEST
            elif predicate == "felt_about":
                claim_modality = ClaimModality.FEELING
            elif predicate == "believed":
                claim_modality = ClaimModality.BELIEF
            claims.append(
                self._claim(
                    entry,
                    sentence,
                    start,
                    end,
                    sender_key,
                    predicate,
                    object_type,
                    surface,
                    object_key,
                    claim_modality,
                    sender_key,
                    observed,
                    time,
                    polarity,
                )
            )

        unique_aliases = {alias["key"]: alias for alias in aliases}
        return ExtractionBatch(
            aliases=tuple(unique_aliases.values()),
            claims=tuple(claims),
        )

    @staticmethod
    def _sender_alias(entry: Entry, sender: str) -> tuple[str, dict]:
        """The identity to file this message's speaker under.

        A conversation's own id is the best handle there is when the chat has
        exactly two people in it, because then the thread *is* the other
        person. In a group it is not, and using it anyway files every speaker
        as one person — measured on this archive, 6 Telegram groups had become
        6 "people", and 84 claims of the active generation's 1,118 were
        attributed to whoever happened to speak in them first.
        """
        addresses = getaddresses([sender])
        email = next((address for _, address in addresses if "@" in address), None)
        label = addresses[0][0] if addresses and addresses[0][0] else sender
        if email:
            key = f"email:{email.casefold()}"
            return key, _alias(
                key, "person", label or email, "email", email, entry.id, verified=True
            )
        if entry.source == "whatsapp" and entry.thread_id and _is_one_to_one(entry):
            jid = entry.thread_id.removeprefix("wa:")
            key = f"whatsapp:{jid.casefold()}"
            return key, _alias(
                key, "person", sender, "whatsapp_jid", jid, entry.id, verified=True
            )
        if entry.source == "telegram" and entry.thread_id and _is_one_to_one(entry):
            stable = entry.thread_id.removeprefix("tg:")
            key = f"telegram:{stable}"
            return key, _alias(
                key, "person", sender, "telegram_id", stable, entry.id, verified=True
            )
        # Nobody's account id is known, so the name they type is the only handle
        # there is — and a display name is never auto-merged, so each speaker
        # arrives as their own identity with the rest offered to
        # `varyz graph review` as merge candidates. That is the conservative
        # direction on purpose: two entities for one person is visible and
        # mergeable, one entity for two people is neither.
        key = f"display:{entry.id}:{sender.casefold()}"
        return key, _alias(
            key,
            "person",
            sender,
            "display_name",
            sender,
            entry.id,
            verified=False,
            confidence=0.65,
        )

    @staticmethod
    def _place_alias(aliases: list[dict], label: str, entry_id: str) -> str:
        key = f"place:{entry_id}:{label.casefold()}"
        aliases.append(
            _alias(
                key,
                "place",
                label,
                "place_spelling",
                label,
                entry_id,
                verified=False,
                confidence=0.8,
            )
        )
        return key

    @staticmethod
    def _project_alias(aliases: list[dict], label: str, entry_id: str) -> str:
        key = f"project:{entry_id}:{label.casefold()}"
        aliases.append(
            _alias(
                key,
                "project",
                label,
                "project_nickname",
                label,
                entry_id,
                verified=False,
                confidence=0.75,
            )
        )
        return key

    @staticmethod
    def _claim(
        entry: Entry,
        quote: str,
        start: int,
        end: int,
        subject_key: str,
        predicate: str,
        object_type: ObjectType,
        surface: str,
        object_key: str | None,
        modality: ClaimModality,
        asserted_by_key: str,
        observed: datetime | None,
        time,
        polarity: str,
    ) -> ClaimCandidate:
        return ClaimCandidate(
            subject_key=subject_key,
            predicate=predicate,
            object_type=object_type,
            object_value=surface,
            object_surface=surface,
            object_entity_key=object_key,
            polarity=polarity,
            modality=modality,
            asserted_by_key=asserted_by_key,
            observed_at=observed,
            observed_tz=entry.tz,
            asserted_at=time.valid_from or time.valid_to,
            valid_from=time.valid_from,
            valid_to=time.valid_to,
            temporal_precision=time.precision,
            temporal_inferred=time.inferred,
            original_time_text=time.original,
            confidence=0.92 if modality == ClaimModality.FACT else 0.8,
            sensitivity=entry.sensitivity,
            plan_state=PlanState.UNKNOWN if modality == ClaimModality.PLAN else None,
            evidence=(
                EvidenceCandidate(
                    entry_id=entry.id,
                    quote=quote,
                    speaker_key=asserted_by_key,
                    char_start=start,
                    char_end=end,
                ),
            ),
        )


class ModelExtractionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aliases: list[dict] = []
    claims: list[ClaimCandidate] = []


class SchemaConstrainedExtractor:
    """Adapter for a locally approved completion function.

    A cloud completion function must itself be supplied through EgressBroker;
    this class has no provider or network dependency.
    """

    name = "schema_model"

    def __init__(
        self,
        complete: Callable[[str, str], str],
        *,
        version: str,
        prompt_version: str,
        model: str,
    ):
        self._complete = complete
        self.version = version
        self.prompt_version = prompt_version
        self.model = model

    def extract(self, entry: Entry) -> ExtractionBatch:
        prompt = (
            "Return strict JSON with aliases and typed claims. Every derived claim must "
            "include an exact evidence quote and offsets. Unknown predicates must still "
            "be returned for quarantine. Preserve modality and unknown time."
        )
        raw = self._complete(prompt, json.dumps(entry.model_dump(mode="json")))
        try:
            envelope = ModelExtractionEnvelope.model_validate_json(raw)
        except ValidationError as exc:
            # Named, not rendered.  ``exc`` here carries ``input_value=``, which
            # is the model's reply to a prompt made of one of the owner's
            # entries, and this message is chained into whatever reports the
            # failure.  ``ValidationError`` is a ``ValueError``, so nothing
            # upstream that catches ``ValueError`` will notice the difference.
            raise ValueError(
                f"invalid graph extraction schema: {refusal_reason(exc)}"
            ) from exc
        return ExtractionBatch(
            aliases=tuple(envelope.aliases), claims=tuple(envelope.claims)
        )


class GraphBuildService:
    def __init__(
        self,
        repository: GraphRepository,
        evidence: EvidenceReader,
        extractor=None,
        cache: ExtractionCache | None = None,
        max_consecutive_failures: int = 5,
    ):
        self.repository = repository
        self.evidence = evidence
        self.extractor = extractor or DeterministicExtractor()
        self.cache = cache
        self.max_consecutive_failures = max_consecutive_failures

    def _extract_twice(self, entry: Entry) -> ExtractionBatch | Exception:
        """One retry, then give up on this entry rather than on the run.

        A single `ReadTimeout` from the model used to re-raise and fail the
        whole generation — hours of staged work discarded because one prompt
        was slow. Extraction is a per-entry question, so a failure is answered
        per entry: try twice, then hand back the error for quarantine.
        """
        for attempt in (1, 2):
            try:
                return self.extractor.extract(entry)
            except Exception as exc:
                if attempt == 2:
                    return exc
        raise AssertionError("unreachable")

    def rebuild(
        self,
        *,
        version: str | None = None,
        dry_run: bool = False,
        should_stop: Callable[[], bool] | None = None,
        allow_downgrade: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> RebuildResult:
        extractor_version = version or self.extractor.version
        corpus = self.evidence.corpus_fingerprint()
        corpus_hash = corpus.content_hash
        # The identity rules are part of the reading: who a claim is about is as
        # much of an answer as what it says, and correcting a rule that files a
        # group chat as a person changes the graph without changing a word of
        # the archive. Left out of this key, that correction could not name a
        # generation and a rebuild answered "no-op" with the defect still in it.
        # It costs nothing to re-read for: identity is outside the *cache* scope
        # and recomputed per entry, so the new generation is built from
        # extractions already paid for.
        generation_key = hashlib.sha256(
            (
                f"{self.extractor.name}:{extractor_version}:"
                f"{self.extractor.model}:{self.extractor.prompt_version}:"
                f"identity{self.repository.identity_version}:"
                f"{corpus_hash}"
            ).encode()
        ).hexdigest()
        if dry_run:
            return RebuildResult(
                run_id=None,
                generation_key=generation_key,
                entries_total=corpus.entries,
                entries_processed=0,
                claims=0,
                aliases=0,
                quarantined=0,
                no_op=False,
                dry_run=True,
            )
        run, no_op = self.repository.begin_run(
            generation_key=generation_key,
            extractor_name=self.extractor.name,
            extractor_version=extractor_version,
            model=self.extractor.model,
            prompt_version=self.extractor.prompt_version,
            input_content_hash=corpus_hash,
            entries_total=corpus.entries,
        )
        if no_op:
            return self.repository.run_result(run.id, no_op=True)

        reused = 0
        failed = 0
        consecutive = 0
        # Entries read so far, a resumed run's earlier ones included, for a
        # caller that shows how far a long first build has got.
        done = run.entries_processed
        try:
            # Streamed, not materialised: the walk resumes from the checkpoint
            # in SQL, so the archive is never held in memory all at once.
            for entry in self.evidence.iter_entries(after_id=run.checkpoint_entry_id):
                # Checked per entry rather than per run: staging commits one
                # checkpoint at a time, so stopping here loses no work and the
                # next run picks up at the same entry.
                if should_stop is not None and should_stop():
                    return replace(
                        self.repository.run_result(run.id),
                        entries_reused=reused,
                        entries_failed=failed,
                        stopped=True,
                    )
                batch = self.cache.get(entry) if self.cache is not None else None
                if batch is None:
                    outcome = self._extract_twice(entry)
                    if isinstance(outcome, Exception):
                        consecutive += 1
                        failed += 1
                        if consecutive >= self.max_consecutive_failures:
                            raise ExtractionUnavailable(
                                f"{consecutive} consecutive extraction failures, "
                                f"last {type(outcome).__name__}"
                            ) from outcome
                        # Quarantined rather than skipped: the checkpoint moves
                        # past this entry, so a resume does not re-run it, and
                        # the generation records that it has a hole.
                        self.repository.stage_batch(
                            run.id, entry, _extraction_failure(outcome)
                        )
                        done += 1
                        if on_progress is not None:
                            on_progress(done, corpus.entries)
                        continue
                    batch = outcome
                    consecutive = 0
                    if self.cache is not None:
                        self.cache.put(entry, batch)
                else:
                    reused += 1
                    # Who spoke is re-answered over every cached batch: it costs
                    # nothing, and caching it is what kept a corrected identity
                    # rule from reaching an archive already read.
                    reidentify = getattr(self.extractor, "reidentify", None)
                    if reidentify is not None:
                        batch = reidentify(entry, batch)
                self.repository.stage_batch(run.id, entry, batch)
                done += 1
                if on_progress is not None:
                    on_progress(done, corpus.entries)
            self.repository.activate(run.id, allow_downgrade=allow_downgrade)
        except Exception as exc:
            self.repository.fail(run.id, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            # Answers already paid for outlive an interrupted run: staging is
            # thrown away when a generation is abandoned, the cache is not.
            if self.cache is not None:
                self.cache.flush()
        if self.cache is not None:
            self.cache.sweep()
        return replace(
            self.repository.run_result(run.id),
            entries_reused=reused,
            entries_failed=failed,
        )
