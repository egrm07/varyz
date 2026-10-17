"""Configuration: config.toml holds policy, never secrets.

Secrets live in Windows Credential Manager (see varyz.secrets).
Resolution order for the config file: explicit --config flag, $VARYZ_CONFIG,
./config.toml. Relative data_dir resolves against the config file's directory
(or cwd when no config file exists).
"""

from __future__ import annotations

import os
import tomllib
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from varyz.domain import Sensitivity
from varyz.legacy import HOME_DIR_NAME, archive_path


class Settings(BaseModel):
    """A settings table that refuses a key it does not know.

    config.toml is the one file Varyz invites its owner to edit by hand, and
    pydantic's default was to ignore anything it did not recognise. A typo in
    a privacy setting -- `defualt`, `patters`, `[sensitivty]` -- was therefore
    accepted in silence and the shipped default stayed in force, which is the
    one place where reading the file and doing nothing is worse than refusing
    to start. Unknown keys are named and refused instead; see load_config.
    """

    model_config = ConfigDict(extra="forbid")


class StorageConfig(Settings):
    data_dir: Path = Path("data")


# Abstention thresholds measured per embedding model: the distance below which
# a passage still answers the question. Derived on a golden set's dev split and
# confirmed on its held_out split — see reports/RETRIEVAL_BASELINE_2026-07-31.md.
# A model absent from this table has no measured value, and none is invented.
CALIBRATED_ABSTAIN_DISTANCE = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 4.4,
}

# How large the corpus was when that number was measured, and the reason the
# number is not as absolute as it looks. The distance being compared is a
# *minimum* over the whole index, and the minimum of a smaller sample is
# systematically larger — so a threshold read off one corpus quietly encodes
# that corpus's size, and applying it to a smaller one abstains on questions
# the corpus can answer perfectly well.
#
# Measured on this index by treating the k-th nearest neighbour as the nearest
# neighbour of an index k times smaller. Share of answerable golden questions
# wrongly abstained on:
#
#     11,389 vectors   0%      2,278 vectors   10%
#      5,694 vectors   2%      1,000 vectors   28%
#      3,796 vectors   5%        500 vectors   38%
#      2,847 vectors   8%        250 vectors   60%
#
# A user who installs varyz and indexes one project folder lands at the bottom
# of that table, where the tool refuses most of what it could answer. That is
# the worse failure — a weak result is visible and correctable, a withheld one
# is neither — so below the floor, abstention turns itself off.
# The other end of the same drift, measured 2026-08-30 on a 53,910-vector
# archive: the threshold does not merely loosen, the signal it reads goes away.
# `evaluate_retrieval.py --calibrate` on that corpus reports NO SEPARATING
# THRESHOLD -- ten of twenty-five answerable golden questions sit at or beyond
# the nearest unanswerable one. A sentence embedding measures the shape of a
# sentence, and on a large archive an unanswerable question has the shape of a
# hundred answerable ones. No number on this axis fixes that, which is why
# `names_the_archive_has_never_seen` reads a different one.
CALIBRATION_CORPUS_VECTORS = 11_389
# Set where the measured error is still ~5%. Not at the calibration size
# itself: that would leave the eval corpus sitting exactly on the boundary,
# where one ingested file decides whether abstention runs at all.
MIN_CORPUS_VECTORS_FOR_ABSTENTION = 3_800

# Below that floor the fallback is lexical coverage — the share of a question's
# content words that appear anywhere in the index — which was measured on a
# 247-vector corpus and separates answerable from unanswerable cleanly there:
# 9/9 answered, 8/8 refused.
#
# It is less size-dependent than distance but not free of it: a corpus of three
# notes contains the vocabulary of no question, on-topic or not, so every
# question scores near zero and every question would be refused. So the lexical
# gate needs a floor of its own. Two points are measured — it works at 247
# vectors and refuses everything at ~10 — and nothing in between, so this is
# set just below the working one rather than pretending to a precision the two
# samples do not support. Below it no gate runs at all: an archive that small
# was installed minutes ago, and refusing everything is the one answer
# guaranteed to be wrong.
MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE = 200


class EmbeddingConfig(Settings):
    # Changing this re-embeds the corpus on the next `varyz embed`, and rebuilds
    # the vector index if the new model has a different width. There is no
    # `dims` setting: the width is taken from the model itself, because a number
    # typed here that disagreed with the model would be believed right up to the
    # first insert and then fail with a bare SQLite error.
    model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # A budget of zero is not a small chunk, it is no room to cut at all: the
    # splitter has to take a piece off an over-long paragraph before it can
    # look at the rest, and with nothing to take it looks at the same
    # paragraph again. `varyz sync` hung there -- no output, no error, no end
    # -- on a number a reader could plausibly type into the one file Varyz
    # invites them to edit by hand.
    chunk_tokens: int = Field(default=300, ge=1)


class ProviderModels(Settings):
    chat_model: str
    enrich_model: str


class LLMConfig(Settings):
    # Which account answers `ask` and runs enrichment: claude | openai | deepseek.
    # Set by `varyz connect <provider>`; sensitivity gating applies to all equally.
    provider: str = "claude"
    monthly_spend_cap_usd: float = 25.0
    claude: ProviderModels = Field(
        default_factory=lambda: ProviderModels(
            chat_model="claude-opus-5", enrich_model="claude-haiku-4-5"
        )
    )
    openai: ProviderModels = Field(
        default_factory=lambda: ProviderModels(chat_model="gpt-5.5", enrich_model="gpt-5.5")
    )
    deepseek: ProviderModels = Field(
        default_factory=lambda: ProviderModels(
            chat_model="deepseek-chat", enrich_model="deepseek-chat"
        )
    )

    # Legacy aliases (pre-provider configs); used as claude overrides when present.
    chat_model: str | None = None
    enrich_model: str | None = None
    summary_model: str | None = None

    def models_for(self, provider: str | None = None) -> ProviderModels:
        name = provider or self.provider
        models: ProviderModels = getattr(self, name)
        if name == "claude" and (self.chat_model or self.enrich_model):
            return ProviderModels(
                chat_model=self.chat_model or models.chat_model,
                enrich_model=self.enrich_model or models.enrich_model,
            )
        return models


class TimelineConfig(Settings):
    # Entries at or above this significance appear in the default timeline
    # view. Everything remains in search and ask regardless.
    threshold: float = 0.55


class OllamaConfig(Settings):
    """Compatibility backend for users who already operate Ollama."""

    model: str = "gemma4:12b-it-q4_K_M"
    url: str = "http://localhost:11434"


class LocalModelConfig(Settings):
    """Varyz-owned local inference; no separately installed app is required."""

    backend: Literal["managed", "ollama"] = "managed"
    model: str = "gemma4:12b-it-q4_K_M"
    model_path: Path | None = None
    model_url: str = (
        "https://registry.ollama.ai/v2/library/gemma4/blobs/"
        "sha256:1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606"
    )
    model_sha256: str = "1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606"
    model_size_bytes: int = 7_381_382_048
    runtime_release: Literal["b10221"] = "b10221"
    # `auto` picks CUDA when an NVIDIA GPU is present and Vulkan otherwise. The
    # CUDA runtime is a 537 MB download against Vulkan's 34 MB, so it is only
    # fetched on a machine that can use it, and Vulkan remains the fallback
    # when the CUDA build refuses to start.
    runtime_variant: Literal["auto", "cpu", "vulkan", "cuda"] = "auto"
    context_window: int = Field(default=8192, ge=1024, le=262_144)
    # Slots served concurrently, each given a full `context_window` of its own.
    # Extraction gains nothing from them: it stages entries in checkpoint order
    # and asks one question at a time, and four concurrent prompts measured 1.00
    # against 0.84 prompts/s. Annotating the personal model does, because it has
    # tens of thousands of independent episodes to get through and sends one
    # group per slot; four slots measured 2.8x the aggregate generation of one.
    # The default stays at 1 because the slots multiply the window and it is the
    # window, not the weights, that decides whether the card can hold it: four
    # slots at a 16k window needs about 10.8 GB. Raise it on a card with room.
    parallel_slots: int = Field(default=1, ge=1, le=16)
    # How many of the model's layers the card holds. `auto` lets llama.cpp
    # decide, which it does from the memory free at the moment it loads, and on
    # a machine whose desktop is also on the card that is a decision made with
    # the wrong number. A count leaves the remainder on the CPU: the weights
    # are 7,024 MiB across 48 layers, so each layer withheld is about 146 MiB
    # back, bought at whatever that layer costs to compute on the CPU. This
    # exists to be turned down on a card that has been seen to die with memory
    # apparently to spare, and it is not a substitute for knowing why.
    gpu_layers: int | Literal["auto"] = "auto"
    # Whether the CUDA backend allocates through `cudaMallocManaged`, so that
    # a buffer the card cannot hold is paged from host memory instead of the
    # launch failing.
    #
    # This is what stood between this machine and a working synthesis stage.
    # With two slots the server loaded, served every request sent to slot 1,
    # and then died with `CUDA error: out of memory` in
    # `ggml_cuda_kernel_launch` on the first request dispatched to slot 0 --
    # reproducibly, four runs out of four, at 8,211 MiB of 12,227 used. Four
    # gigabytes free is not a card that is out of memory, which is why every
    # earlier reading of this failure was wrong: it was blamed on prompt size,
    # then on slot count, then on other applications holding the card, and the
    # numbers never supported any of them. Disabling programmatic dependent
    # launch (`GGML_CUDA_PDL=0`) moved the error one line and changed nothing,
    # nor did withholding four layers.
    #
    # What it costs, measured on the same preflight: 173s against the 98-134s
    # of the runs that died, so call it 40% slower. That is the price of an
    # allocation that fails being served from host memory rather than ending
    # the run, and a stage that finishes slowly finishes.
    cuda_unified_memory: bool = True
    # What that costs, which took until 2026-09-18 to see. A unified-memory
    # allocation reaches into host RAM and the driver pins the pages behind it,
    # and on Windows a server is stopped with `TerminateProcess` -- there is no
    # gentler signal -- so llama.cpp never runs the code that would free them.
    # The pages survive the process, belong to no process afterwards, and only
    # a driver reload or a reboot returns them. A night of phases, each one
    # starting a server and killing it, took the machine from 19.5 GB free to
    # 0.6 GB of 31.1 while every process on it together held 5 GB.
    #
    # The runtime now asks a server to stop and waits for it, which is the fix;
    # this floor is the seatbelt. Refuse to start below this much free physical
    # memory rather than produce what comes next, which is a machine paging
    # through a model's weights and a phase that reads as merely slow until it
    # dies. 0 disables the check. See `_require_host_memory` in the runtime.
    host_memory_floor_mib: int = Field(default=2_048, ge=0, le=131_072)
    # What the attention caches are stored as. Gemma keeps 40 of its 48 layers
    # on a fixed 1,024-token window, so the caches barely grow with the context
    # window but are paid for once per slot: 736 MiB a slot at 16k, against
    # 7,024 MiB of weights. `q8_0` halves that, which is worth 4% less on its own
    # — dequantising costs more than the spill it avoids — and 14% more once the
    # freed memory is spent on two further slots. It is still a change to what
    # the model computes with, so it is opt-in rather than a default.
    # Quantising the value cache requires flash attention, which the CUDA build
    # enables by itself; on a backend without it, leave this at f16.
    kv_cache_type: Literal["f16", "q8_0", "q5_1", "q4_0"] = "f16"
    # Host memory the server may hold saved slot states in, so that a slot given
    # a prompt resembling one it has already read restores the shared prefix
    # instead of reading it again.
    #
    # Annotation gets very little from it, because there is very little to get:
    # a call carries eight distinct episodes behind one 558-token instruction,
    # so the only prefix two calls share is that instruction. Measured on the
    # annotation exchange, the best-matching slot shared a median 0.150 of its
    # prompt and never more than 0.207 -- the system prompt is 9% of a 6,156
    # token call, and the cache is finding that and nothing else. (Across the
    # whole server log the median is 0.566, but that is extraction and `ask`,
    # whose prompts genuinely repeat; it is not this workload and was misread
    # as this workload once already.)
    #
    # What it costs is the problem. llama.cpp defaults to 8,192 MiB on the
    # assumption that it owns the machine, and it does not: the compiler holds
    # 10-12 GB of episodes while this runs, and one saved state at a 16k window
    # is 250-1,728 MiB. Eight gigabytes of those plus the compiler is more than
    # 32 GB of host memory can give, and the allocation that fails takes the
    # server with it. Measured over one archive: 6,915 evictions, four
    # `bad allocation` failures and 164 launches, each crash losing whatever was
    # in flight across all six slots.
    #
    # 4,096 is the smallest cap that still holds two or three states, which is
    # already more than a 0.150 reuse can use, and it leaves the headroom the
    # crashes were taking. 0 turns the cache off, as it does for llama.cpp.
    # The `recall` judge runs that way: its second look continues the slot's
    # own conversation, which needs no saved state, and a server an agent's
    # questions keep alive should not hold gigabytes of memory for nothing.
    prompt_cache_ram_mib: int = Field(default=4096, ge=0, le=131_072)
    # Weights stay resident between requests, which is right during a rebuild
    # and wrong for the 15 minutes afterwards: 7.4 GB of VRAM held by a daemon
    # that will not ask another question until the next cycle. 0 disables the
    # timer and keeps the old behaviour of unloading only at process exit.
    idle_unload_seconds: float = Field(default=180.0, ge=0, le=86_400)
    # The server's own idle release, which works where the timer above cannot:
    # the timer lives in the process that started the server, and a server
    # adopted, detached or orphaned by a process that has exited has no timer
    # at all. After this long without a request llama-server unloads the model
    # from the card and from host memory (8.7 GB of VRAM down to 0.9, 1.6 GB
    # resident down to 0.1) and keeps listening; the next request loads it
    # again, in about the time a fresh start takes, and is answered. What is
    # left of the weights sits in the system's file cache, which Windows gives
    # back as soon as anything else asks for memory. 0 keeps the model loaded
    # until the server stops.
    #
    # Set to match `idle_unload_seconds`, so a server this process owns is
    # stopped rather than left asleep, and sleeping is what happens to the
    # ones it does not own. Sleeping earlier would free the card sooner and
    # cost answers: `recall` does not wait for a sleeping model, it answers
    # without it (16 of 27 benchmark questions rather than 21) and loads it
    # for the next question.
    sleep_idle_seconds: int = Field(default=180, ge=0, le=86_400)
    request_timeout_seconds: float = Field(default=600.0, ge=30, le=3600)
    startup_timeout_seconds: float = Field(default=600.0, ge=30, le=1800)


class RetrievalConfig(Settings):
    # `ask` retrieves candidate_k entries locally, then sends AT MOST send_k
    # short excerpts to the cloud model — never the whole archive.
    candidate_k: int = 24
    send_k: int = 8
    deep_initial_send_k: int = 12
    max_candidate_k: int = 80
    max_send_k: int = 18
    # Local-LLM distillation is never an implicit side effect of `ask`. A
    # managed Gemma launch reserves several gigabytes of GPU and host memory;
    # on one Windows machine an ordinary question triggered a GPU watchdog and
    # an unclean reboot after the answer. `off` is therefore the safe default.
    # `auto` remains accepted as a backwards-compatible alias for off. The CLI
    # requires --local-distill on the individual call before it constructs a
    # local provider; `on` records the owner's preference but is not permission
    # for a background or read command to start the model.
    distill: Literal["auto", "on", "off"] = "off"
    # Abstention floor: the largest embedding distance at which the best
    # matching passage still counts as an answer. Above it, varyz returns
    # nothing rather than its least-bad guess.
    #
    # Distance is on the embedding model's own scale, so a number calibrated
    # for one model is not merely imprecise for another — it is unrelated.
    # Leave this unset and the floor is looked up by model name; a model with
    # no calibration on record disables abstention rather than applying a
    # threshold that has never been measured, because silently discarding real
    # answers is a worse failure than the one abstention prevents. Calibrate a
    # new model with:
    #   uv run python scripts/evaluate_retrieval.py <golden> --calibrate
    abstain_distance: float | None = None
    # A term matching this few entries is an identifier, not a topic. An exact
    # hit on one is worth answering on even when the vectors are unconvinced —
    # nobody types a filename or an error code by accident.
    rare_term_max_entries: int = 20
    # Second look before `recall` answers: a local cross-encoder rereads each
    # candidate against the question and follows the strongest ones to the
    # records they belong with. auto = use it once the model is on disk
    # (the first index build fetches it); on = fetch on first use; off = first-stage
    # ranking only.
    rerank: Literal["auto", "on", "off"] = "auto"
    rerank_model: str = "BAAI/bge-reranker-base"
    # Last look: the owner's local model reads the strongest records and says
    # which answer the question, then reads the records its choices lead to.
    # auto = use it when the local model is installed and has a GPU to run on,
    # else the `[workflows]` classifier model through Ollama on a GPU;
    # on = whenever it is configured; off = the reranker's cut alone. Only
    # a local model is ever asked, and a failure falls back to the cut.
    judge: Literal["auto", "on", "off"] = "auto"


# Sources that are personal by what they are, not by what any one entry says.
# AI conversations are not among them: the owner's turns to coding agents are
# how they build, which is what Varyz exists to hand back to those agents (owner
# decision, 2026-09-27). Secrets in them are redacted on the way out, and the
# content patterns below still hold back a turn that names one of those topics.
#
# varyz no longer reads chat apps, contacts or browsing history (owner decision,
# 2026-09-27): it is about how the owner builds, not their private life. Those
# sources keep their level here only so rows an older archive still holds stay
# shut until the owner deletes them.
REMOVED_SOURCES = frozenset(
    {"browser_history", "google_contacts", "instagram", "telegram", "whatsapp"}
)
BUILTIN_SOURCE_DEFAULTS: dict[str, Sensitivity] = {
    "personal_archive": Sensitivity.SENSITIVE,
    **{source: Sensitivity.SENSITIVE for source in sorted(REMOVED_SOURCES)},
}

# Content that carries its own sensitivity wherever it turns up. These live in
# code, not only in config.example.toml, because an installed copy writes its
# own config from a template and would otherwise ship no content-based
# escalation at all -- an IBAN or a therapist's name in a document staying
# cloud-eligible. Set patterns = [] to turn the whole layer off deliberately.
BUILTIN_SENSITIVE_PATTERNS: list[str] = [
    r"\btherap(y|ist)\b",
    r"\bdiagnos(is|ed)\b",
    r"\bmedication\b",
    r"\bsalary\b",
    r"\b(iban|account number)\b",
]


class SensitivityRules(Settings):
    patterns: list[str] = Field(default_factory=lambda: list(BUILTIN_SENSITIVE_PATTERNS))


class SensitivityConfig(Settings):
    default: Sensitivity = Sensitivity.PERSONAL
    source_defaults: dict[str, Sensitivity] = Field(
        default_factory=lambda: dict(BUILTIN_SOURCE_DEFAULTS)
    )
    rules: SensitivityRules = Field(default_factory=SensitivityRules)

    @field_validator("source_defaults", mode="before")
    @classmethod
    def _name_a_source_without_unnaming_the_rest(cls, value: Any) -> Any:
        """A source_defaults table replaced this mapping instead of amending it.

        The packaged config named ai_conversations and nothing else, so every
        installed copy silently downgraded the chat sources varyz read then to
        cloud-eligible. Naming a source now
        overrides that source; the rest keep the level they are shipped with,
        and lowering one stays possible by naming it.
        """
        if not isinstance(value, dict):
            return value
        merged: dict[str, Any] = dict(BUILTIN_SOURCE_DEFAULTS)
        merged.update(value)
        return merged


def _default_graph_extractor() -> str:
    # Imported here: local_runtime imports this module.
    from varyz.llm.local_runtime import default_graph_extractor

    return default_graph_extractor()


class GraphConfig(Settings):
    # contextual_facets, or deterministic in a build that holds the managed
    # model back (docs/SHIPPING.md), which could not build the default.
    extractor: str = Field(default_factory=_default_graph_extractor)
    semantic_provider: Literal["hybrid", "local"] = "local"
    # 6: extraction reads Russian and Spanish as well as English. The version is
    # part of the generation key, so without the bump a rebuild on an unchanged
    # archive is a no-op and the wider reading never lands.
    # 7: only entries with a speaker reach the model — a document is no longer
    # read as if its prose were the user's own testimony.
    # 8: a claim's object stops at the clause that follows it. Under 7, "I moved
    # to Lisbon in March 2025 and I have been renting in Alfama since" filed a
    # place called the whole of that, which nothing can ever match again. The
    # bump is what makes the correction land: the reading is part of the
    # generation key, and without it `varyz graph rebuild` answers "no-op" and
    # leaves the run-on places in place. It costs a full re-extraction, which
    # is resumable and runs at the archive's own pace.
    # 9: what somebody says inside a working session is kept, labelled "work",
    # and refused by the mirror rather than by the extractor -- and a commit
    # the owner wrote has a speaker after all. Under 8, 1,588 working sessions
    # produced 25 claims and 2,280 commits produced 1, so the graph could not
    # answer a question about the owner's own work, which is most of what they
    # asked it. Both readings are part of the generation key and neither lands
    # without the bump.
    extractor_version: str = "9"
    # Reuse a previous generation's extraction for entries whose content, label
    # and conversation window are unchanged. Off means every rebuild re-reads
    # the whole archive, which is what a new generation used to cost.
    extraction_cache: bool = True
    predicate_version: str = "1"
    # 2: a thread id identifies the person on the other end only when there is
    # exactly one of them. Under 1 a group chat became a person and everything
    # said in it was filed under whoever spoke first.
    identity_version: str = "2"
    candidate_threshold: float = 0.55
    context_before: int = Field(default=6, ge=0, le=30)
    context_after: int = Field(default=2, ge=0, le=10)
    context_max_gap_hours: float = Field(default=12, gt=0, le=168)
    batch_max_messages: int = Field(default=160, ge=3, le=200)
    batch_max_targets: int = Field(default=20, ge=1, le=24)
    semantic_min_confidence: float = Field(default=0.80, ge=0, le=1)
    # How many activated generations to keep behind the live one, so a bad
    # activation can be rolled back to a graph that was once trusted. Failed
    # runs, abandoned staging runs and anything older are deleted after a
    # successful rebuild — nothing used to delete them, and thirteen
    # generations had accumulated 485,251 alias rows to serve one live graph.
    keep_generations: int = Field(default=2, ge=1, le=20)
    # Completed job rows are an audit trail with no reader after this long.
    keep_job_days: int = Field(default=30, ge=1, le=3650)
    prune_on_sync: bool = True
    # Autosync ingests every 15 minutes because ingestion is incremental and
    # cheap. A rebuild is not: three new messages restage the whole archive
    # into a fresh generation. So the graph follows its own cadence — rebuild
    # once this many entries have accumulated since the live generation was
    # built, or once it is this old, whichever comes first. `varyz graph
    # rebuild` ignores both.
    rebuild_min_new_entries: int = Field(default=50, ge=1, le=100_000)
    rebuild_max_age_minutes: int = Field(default=60, ge=1, le=10_080)
    # An entry the extractor failed twice on is quarantined and the run goes on.
    # This many consecutive failures means the model is gone, not that one
    # prompt was slow, and staging thousands of empty batches over a dead model
    # would produce a generation that looks built and says nothing.
    max_consecutive_failures: int = Field(default=5, ge=1, le=1000)
    # Sources retained as archive ground truth but omitted from semantic graph
    # extraction and every personal-model compile derived from that graph.
    # This is deliberately non-destructive: changing the list and rebuilding
    # makes the source eligible again without re-ingestion.
    excluded_sources: list[str] = Field(default_factory=list)

    @field_validator("excluded_sources")
    @classmethod
    def normalize_excluded_sources(cls, value: list[str]) -> list[str]:
        return sorted({item.strip().casefold() for item in value if item.strip()})


class ConversationsConfig(Settings):
    """Reading conversations: whether the daemon does it, and how much at a time.

    Partitioning is deterministic and costs seconds, so it always runs. Reading
    costs about a second of GPU per conversation, which is why it is bounded
    here rather than left to finish: a sync that ingests a year of agent
    sessions would otherwise hold the card for an hour on a machine whose owner
    is trying to use it. The cap is per cycle and the pass is resumable, so a
    large archive is read over several syncs and no single one is long.
    """

    # Off means chat history is only read when the owner asks. It is on
    # because the alternative is an archive that answers about last year and
    # not last week -- and recent is what gets asked about.
    read_on_sync: bool = True
    # Roughly four minutes of model time on the machine this was measured on.
    max_per_sync: int = 200
    # Which message sources are partitioned and read as conversations. A chat
    # app named here by an older config is dropped: varyz does not read them.
    sources: list[str] = Field(default_factory=lambda: ["ai_conversations"])

    @field_validator("sources")
    @classmethod
    def normalize_sources(cls, value: list[str]) -> list[str]:
        names = {item.strip().casefold() for item in value if item.strip()}
        return sorted(names - REMOVED_SOURCES)


class OwnerConfig(Settings):
    """What the archive's owner is called, when they have said so.

    Empty by default, and nothing depends on it being filled in. Its one job is
    to catch messages a connector filed as the owner's own that are plainly
    addressed *to* them: a whatsapp entry headed `From: me` whose body reads
    "Thank you for everything Egor, I owe you one" put somebody else's
    declaration into his Mirror as one of his own values. People do not address
    themselves by name, and until varyz is told the name there is nothing in
    such a message to tell it apart from one he wrote.
    """

    names: list[str] = Field(default_factory=list)


class AgentConfig(Settings):
    """How an agent reaching varyz through MCP should present what it returns.

    Presentation only. The rules that keep an answer honest — evidence behind
    every claim, no excerpt passed off as the agent's own conclusion, refusal
    where the archive says nothing — hold at every setting. See
    `varyz.agent_style`, which renders this into the MCP handshake and into any
    routing skill installed beside it, and which reads this key without loading
    this model.
    """

    citations: Literal["minimal", "full", "off"] = "minimal"
    # Hooks installed by `varyz connect` hand the agent its briefing (how the
    # owner works, this project's decisions, the definition of done) with the
    # user's first prompt, so it arrives even when the agent forgets to ask.
    briefing_hooks: bool = True
    # Before the agent ends a turn in which it changed files, it is sent back once
    # to walk the owner's definition of done. Off turns the gate, not the briefing, off.
    done_gate: bool = True
    # A small change to work already there (a fix, a tweak, a wording) skips the
    # gate; `check` walks the definition of done after every edit turn as before.
    done_gate_small_changes: Literal["skip", "check"] = "skip"


class WorkflowsConfig(Settings):
    """How a task is sized before an agent raises an open workflow step.

    Only new work at a later step is raised; a small change is just done. The
    call is made on this machine, never elsewhere: the input is the owner's
    prompt. See `varyz.resolution.task_size` and
    reports/TASK_SIZE_CLASSIFIER_2026-09-25.md for the measurements.
    """

    # model: the Ollama model below (98.6% on the held-out set), falling back to
    # the embedding when Ollama is down, the model is not pulled, it would crowd
    # the GPU, or it does not answer in time. embedding: no GPU (89.9%).
    # lexicon: verbs only (60.9%).
    # Setup records an explicit mode. Legacy preserves existing installations.
    inference_mode: Literal["legacy", "local", "conversations"] = "legacy"
    task_classifier: Literal["model", "embedding", "lexicon"] = "model"
    # The same local model answers four more closed questions, each falling back
    # the same way and ending at the rule that was there before. See
    # reports/LOCAL_CLASSIFIER_ROLLOUT_2026-09-25.md for each one's measurements.
    # Does a task redo a step already built (workflow rework)?
    redo_classifier: Literal["model", "embedding", "rule"] = "model"
    # Is a prompt work or a question (the hooks brief only work)?
    prompt_classifier: Literal["model", "embedding", "rule"] = "model"
    # Is an owner answer a habit for every project or a choice for this one?
    # The model must be sure before it calls anything general (fail narrow).
    scope_classifier: Literal["model", "rule"] = "model"
    # When no stored owner answer matches an `ask` word for word, the model
    # checks the closest few; off leaves only the strict lexical matcher.
    paraphrase_verifier: Literal["model", "off"] = "model"
    # Before an `ask` is answered from archive passages, the model reads the top
    # few against the question; off leaves only the word-coverage check.
    evidence_verifier: Literal["model", "off"] = "model"
    # `varyz rules mine` reads each of the owner's turns with the model to find
    # standing rules; lexical keys on words like "always" and "I want", which
    # mostly finds requests for the task in hand. The model falls back to it.
    rule_miner: Literal["model", "lexical"] = "model"
    # Whether the cloud model `[llm] provider` names reads those turns, through
    # the sign-in `varyz connect` made. On by default: it reads first and the
    # local model takes over when it cannot; `varyz rules mine --no-cloud` reads
    # on this machine only. Credentials are removed and sensitive turns never go.
    rule_reader_cloud: bool = True
    # At most this many cloud calls a run, 20 turns each; the rest wait.
    rule_reader_cloud_max_calls: int = Field(default=30, ge=1)
    # Whether start_task asks the agent the owner works in to have a subagent on
    # its cheapest model read those turns, through the agent's own sign-in. The
    # same turns go as to the cloud reader; what comes back waits for review.
    rule_reader_agent: bool = True
    # Live reading: each prompt pulls the newest messages from the AI chats into
    # the archive, and the briefing asks for that subagent whenever there are
    # messages from the last `live_reading_hours` it has not read, at most once
    # every `live_reading_min_minutes` -- at once when the prompt states a
    # standing rule ("from now on ..."). Only those recent messages are read.
    live_reading: bool = True
    live_reading_min_minutes: int = Field(default=5, ge=0)
    live_reading_hours: int = Field(default=24, ge=1)
    # Who starts that reader. auto: varyz itself, in the background, on the
    # owner's Claude Code or Codex sign-in -- whichever has more of its limits
    # left -- with only the two reading tools (`varyz.resolution.headless_reader`).
    # claude / codex: that one only. agent: the briefing asks the working agent.
    reader_runner: Literal["auto", "claude", "codex", "agent"] = "auto"
    # "haiku" is Claude Code's name for its newest, cheapest model; "auto" takes
    # Codex's newest luna (cheapest) model the account may use.
    reader_claude_model: str = "haiku"
    reader_codex_model: str = "auto"
    # Legacy options remain readable for existing installs; allowance thresholds
    # no longer control learning. Only an actual provider refusal pauses it.
    large_reading_min_left_5h: float = Field(default=0, ge=0, le=100)
    large_reading_min_left_weekly: float = Field(default=0, ge=0, le=100)
    small_reading_min_left_5h: float = Field(default=0, ge=0, le=100)
    small_reading_min_left_weekly: float = Field(default=0, ge=0, le=100)
    # How often the limits are read while a reader runs (Claude's also arrive
    # with each of its model calls), how long one may run, and how many tasks
    # one reader does before a fresh one takes over (its context stays small).
    reader_usage_poll_seconds: int = Field(default=60, ge=15)
    reader_max_minutes: int = Field(default=90, ge=1)
    reader_tasks_per_run: int = Field(default=8, ge=1)
    # Trial rules: a rule the reader found with strong evidence (the owner
    # corrected an agent over it, or said it in two conversations or projects)
    # goes into effect at once, marked as not yet the owner's word; the rest wait
    # for review. See `varyz.resolution.trials`.
    trial_rules: bool = True
    # Legacy review exposure thresholds. Exposure never confirms an owner rule;
    # only an explicit keep or owner correction ends a trial.
    trial_promote_tasks: int = Field(default=3, ge=1)
    trial_promote_days: int = Field(default=7, ge=0)
    # `varyz rules review` shows this many trial rules, at random, to keep or drop.
    trial_spot_check: int = Field(default=10, ge=0)
    # No new rule goes on trial while the spot-check keeps fewer than this share.
    trial_min_kept: float = Field(default=0.9, ge=0, le=1)
    classifier_model: str = "qwen3.5:4b"
    classifier_url: str = "http://localhost:11434"
    # Covers loading the model the first time (3.9 s measured); once loaded it
    # answers in about 150 ms.
    classifier_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # The share of the card that must stay free after the model loads. Below
    # it, the embedding answers instead, so a running job keeps the GPU.
    classifier_vram_headroom: float = Field(default=0.3, ge=0, lt=1)
    # How long the model stays loaded after its last answer (Ollama keep_alive,
    # e.g. "10m", "90s", "1h"). 10 minutes found it loaded for 75% of the owner's
    # prompts; every minute longer holds ~3 GB of VRAM a GPU job may want.
    classifier_keep_alive: str = Field(default="10m", pattern=r"^[1-9][0-9]*[smh]$")

    @field_validator("classifier_url")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        from urllib.parse import urlsplit

        if urlsplit(value).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "classifier_url must be on this machine (localhost): the classifier "
                "reads the owner's prompts"
            )
        return value


class PrivacyConfig(Settings):
    mode: Literal["shadow", "enforce"] = "enforce"
    policy_version: str = "1"
    receipt_version: str = "1"
    max_context_chars: int = 9000
    minimum_aggregate_size: int = 3
    # Whether an agent reading the Mirror may see the *words* of evidence
    # classified sensitive. Off by default: the conclusion and its citation
    # travel either way, so turning this on buys wording and nothing else,
    # and the wording is the part that is nobody's business. It cannot reach
    # unclassified evidence at any setting.
    mirror_sensitive_quotes: bool = False


class ResolutionConfig(Settings):
    """Local resolution daemon and transport-independent device identity."""

    bind_host: str = "127.0.0.1"
    port: int = Field(default=1207, ge=1, le=65_535)
    auth_mode: Literal["session", "token"] = "session"
    device_id: str = Field(default="local", min_length=1, max_length=200)
    # An agent's first resolve starts a stopped loopback daemon instead of
    # reporting an outage; owner context should not depend on remembering to.
    autostart: bool = True

    @model_validator(mode="after")
    def widened_bind_requires_strong_auth(self) -> ResolutionConfig:
        if self.bind_host not in {"127.0.0.1", "localhost", "::1"} and self.auth_mode != "token":
            raise ValueError("non-loopback resolution bind requires token auth")
        return self


class AutoSyncConfig(Settings):
    # Installation creates an ENABLED marker as the explicit consent boundary.
    # This switch lets a hand-edited config disable both the scheduled and
    # on-use paths without deleting that marker first.
    enabled: bool = True
    on_use: bool = True
    freshness_minutes: int = Field(default=5, ge=1, le=1440)
    schedule_minutes: int = Field(default=15, ge=5, le=1440)
    # How long one cycle may run before it stops at the next checkpoint. A
    # daemon only re-reads its own source code, and only notices the kill
    # switch is gone, *between* cycles — one wedged sync here held the write
    # lock for 3 h 38 min and had to be killed by hand. Staging is checkpointed
    # per entry, so an interrupted cycle loses nothing and the next one
    # resumes. 0 disables the deadline.
    cycle_deadline_minutes: int = Field(default=30, ge=0, le=1440)
    # Contextual graph extraction starts the managed 7.4 GB model. Background
    # freshness must stay safe on a machine whose display shares that GPU, so
    # autosync only runs a semantic rebuild after an explicit opt-in. Ingestion,
    # embedding, and deterministic graphs remain automatic either way.
    semantic_graph: bool = False
    # After new turns to coding agents arrive, learn the order the owner guides
    # work in and hand it to agents (marked as learned, withdrawable with
    # `varyz workflows retract`). Lexical, local, and quick.
    learn_workflows: bool = True
    # Record the option the owner picks in a Claude Code question as that
    # project's decision; a newer pick supersedes. The owner chose this on
    # 2026-09-25. Withdraw one with `varyz knowledge retract`.
    capture_decisions: bool = True
    notify_server: str = "https://ntfy.sh"
    notify_topic: str = ""


class UpdateConfig(Settings):
    # Keep a git checkout on the latest commit of the branch it tracks: checked
    # once a day on the first use of the day, fast-forward only, the database
    # backed up before a migration. "notify" only says an update is there.
    # Off by default: a check is a network call, and the alpha makes none.
    mode: Literal["off", "notify", "auto"] = "off"


class NewsConfig(Settings):
    # Posts from varyzai.com/news, checked once an hour while Varyz is in use and
    # passed to the owner by their agent, each post once. On by default; the
    # switch is `varyz news off`, which writes data/news/OFF.
    url: str = "https://varyzai.com/news.json"


class PromotionConfig(Settings):
    """Local commercial notices: launch and integration review are explicit gates."""

    enabled: bool = False
    cloud_ready: bool = False
    reviewed_agent_ids: list[str] = Field(default_factory=list)


class RuleShadowConfig(Settings):
    """Local experiment bounds; opt-in is data/rules/shadow/ENABLED."""

    max_comparisons_per_day: int = Field(default=500, ge=1, le=10000)
    max_source_records: int = Field(default=10000, ge=1, le=100000)
    max_registry_events: int = Field(default=50000, ge=1, le=1000000)
    max_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)


class SessionReportsConfig(Settings):
    """Backfill bounds never grant Cloud sharing permission."""

    cloud_since: date | None = None


class Config(Settings):
    _source_file: Path | None = PrivateAttr(default=None)

    @property
    def source_file(self) -> Path | None:
        """The exact loaded file, including nonstandard --config filenames."""
        return self._source_file

    storage: StorageConfig = Field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    timeline: TimelineConfig = Field(default_factory=TimelineConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    local_model: LocalModelConfig = Field(default_factory=LocalModelConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    sensitivity: SensitivityConfig = Field(default_factory=SensitivityConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    conversations: ConversationsConfig = Field(default_factory=ConversationsConfig)
    owner: OwnerConfig = Field(default_factory=OwnerConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    workflows: WorkflowsConfig = Field(default_factory=WorkflowsConfig)
    rule_shadow: RuleShadowConfig = Field(default_factory=RuleShadowConfig)
    session_reports: SessionReportsConfig = Field(default_factory=SessionReportsConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    resolution: ResolutionConfig = Field(default_factory=ResolutionConfig)
    autosync: AutoSyncConfig = Field(default_factory=AutoSyncConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    promotions: PromotionConfig = Field(default_factory=PromotionConfig)
    connectors: dict[str, dict[str, Any]] = Field(default_factory=dict)

    base_dir: Path = Path(".")  # directory relative paths resolve against

    @property
    def data_dir(self) -> Path:
        d = self.storage.data_dir
        return d if d.is_absolute() else (self.base_dir / d).resolve()

    @property
    def db_path(self) -> Path:
        # varyz.db, or an archive still named varys.db, renamed once it is safe.
        return archive_path(self.data_dir)

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    def connector_options(self, connector_id: str) -> dict[str, Any]:
        return self.connectors.get(connector_id, {})



def home_config_file() -> Path:
    """The archive `varyz setup` creates when no config is named.

    ~/.varyz/config.toml, or config.toml under $VARYZ_HOME when that is set --
    which the test suite does, so no test ever opens the real one.
    """
    override = os.environ.get("VARYZ_HOME")
    home = Path(override) if override else Path.home() / HOME_DIR_NAME
    return home / "config.toml"


def find_config_file(explicit: Path | None = None) -> Path | None:
    """--config, then $VARYZ_CONFIG, then ./config.toml, then the home archive.

    The last step is what makes `varyz setup` enough: it creates the home
    archive, and every command typed afterwards in any folder without a
    config.toml of its own reads that one instead of stopping with "Run: varyz
    setup" after setup has just run.
    """
    if explicit is not None:
        return _config_target(explicit)
    env = os.environ.get("VARYZ_CONFIG")
    if env:
        return _config_target(Path(env))
    local = Path.cwd() / "config.toml"
    if local.exists():
        return _config_target(local)
    home = home_config_file()
    if home.exists():
        return _config_target(home)
    return None


def _config_target(path: Path) -> Path:
    """Follow an explicit archive alias; never fall back to another archive on error."""
    seen: set[Path] = set()
    for _ in range(8):
        resolved = path.resolve()
        if resolved in seen:
            raise ConfigError(f"Config redirect cycle at {path}")
        seen.add(resolved)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError):
            if len(seen) > 1:
                raise ConfigError(f"Config redirect target cannot be read: {path}") from None
            return path  # load_config retains its existing error handling.
        target = data.get("redirect_config")
        if target is None:
            return path
        if not isinstance(target, str) or not target.strip():
            raise ConfigError(f"redirect_config must name a config file in {path}")
        destination = Path(target).expanduser()
        path = destination if destination.is_absolute() else path.parent / destination
    raise ConfigError("Config redirect chain is too long")


class ConfigError(Exception):
    """A config file that cannot be used, described in terms of the file."""


def load_config(explicit: Path | None = None) -> Config:
    path = find_config_file(explicit)
    if path is None:
        return Config(base_dir=Path.cwd())
    # config.toml is the one file Varyz invites its owner to edit by hand, so
    # its three failure modes -- absent, unparsable, wrong type -- are the ones
    # most likely to be met. Each answered with a Python traceback naming varyz
    # internals, which says nothing about the line that needs changing.
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"No config file at {path}. Run: varyz setup") from None
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    try:
        cfg = Config(**raw, base_dir=path.resolve().parent)
        cfg._source_file = path.resolve()
        return cfg
    except ValidationError as exc:
        problems = "; ".join(_describe(error) for error in exc.errors())
        raise ConfigError(f"{path} has settings Varyz cannot use -- {problems}") from None


def _settings_beside(location: tuple[Any, ...]) -> list[str]:
    """The names of the table an unknown key was written into."""
    model: type[BaseModel] | None = Config
    for part in location[:-1]:
        field = model.model_fields.get(str(part)) if model is not None else None
        annotation = field.annotation if field is not None else None
        model = (
            annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel)
            else None
        )
    return list(model.model_fields) if model is not None else []


def _describe(error: Any) -> str:
    """One validation failure, in terms of the line that needs changing."""
    location = tuple(error["loc"])
    name = ".".join(str(part) for part in location)
    if error["type"] != "extra_forbidden":
        return f"{name}: {error['msg']}"
    # A refused key is almost always a misspelling of a real one, and the name
    # it was reaching for says more than the list it is missing from.
    import difflib

    beside = [known for known in _settings_beside(location) if known != "base_dir"]
    close = difflib.get_close_matches(str(location[-1]), beside, n=1, cutoff=0.6)
    if close:
        return f"{name}: not a setting. Did you mean {close[0]}?"
    return f"{name}: not a setting. Settings here: {', '.join(sorted(beside))}"
