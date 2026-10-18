"""Is a task new work, or a small change to work already there?

A workflow nudge is raised only for new work at a later step. This is the local
classifier that makes that call, in the manner of a Jev decision: one closed
question, answered in one pass by a model on this machine, with a single letter
read from the first token's log-probabilities. Nothing leaves the machine: the
model is reached on loopback only, and a configured address elsewhere is refused.

Three sizers, tried in order, each measured on an authored held-out set that no
prompt or example here was tuned on (``scripts/task_size_bench.py``,
reports/TASK_SIZE_CLASSIFIER_2026-09-25.md):

* ``model`` - a small local model through Ollama, shown eight examples: 98.6%,
  about 150 ms once loaded (4 s to load). It is loaded only when a size is needed and only
  when loading it leaves the card its headroom, so it never pushes a running
  job (the managed model, a training run) off the GPU.
* ``embedding`` - the archive's own sentence embedding against the authored
  examples below, nearest centroid: 89.9%, no GPU.
* ``lexicon`` - the verb lexicon in ``workflows``: 60.9%. The last fallback.

The same chain, with its own question and examples, decides whether a task
redoes a step (``redo_classifier_from_config``). Both run through
``varyz.resolution.local_classifier``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from varyz.resolution.local_classifier import (
    KEEP_ALIVE,
    BinaryClassifier,
    Centroids,
    ClassifierUnavailable,
    LocalModel,
    Question,
    classifier_from_config,
    embedder_from_config,
    gpu_memory_mib,
    loaded_mib,
    local_model_from_config,
    p_first_letter,
)
from varyz.resolution.workflows import Sizing, Step, TaskSizer, is_redo, lexicon_sizer

LETTER_PROMPT = (
    "You sort one request made to a coding or design agent. Answer A if it is new "
    "work: it makes something that is not there yet (a page, a section, a screen, "
    "a feature, a document, a design), or remakes one wholesale. Answer B if it is "
    "a small change: it changes something already there without remaking it (a "
    "fix, a bug, a tweak, a wording, a colour, a size, a rename). Answer with the "
    "single letter A or B."
)

# Authored examples, the dev set of scripts/fixtures/task_size_dev.json and
# nothing else: the embedding's centroids come from all of them, the model's
# examples from the eight in FEW_SHOT. Not from any owner's archive.
EXAMPLES: tuple[tuple[str, str], ...] = (
    ("Build the landing page", "new"),
    ("Make the landing page for persona", "new"),
    ("Let's do the landing page now", "new"),
    ("Write the copy for the pricing page", "new"),
    ("Create an about page with the team bios", "new"),
    ("I need a blog section on the site", "new"),
    ("Put together the homepage", "new"),
    ("Start on the page content", "new"),
    ("Add a pricing page", "new"),
    ("Set up the FAQ page", "new"),
    ("Draft the homepage hero and features sections", "new"),
    ("Can you do the contact page?", "new"),
    ("The site needs a changelog page", "new"),
    ("Design the logo", "new"),
    ("Come up with a logo for san7", "new"),
    ("Redo the landing page from scratch", "new"),
    ("Rebuild the homepage with the new layout", "new"),
    ("Fix the padding on the hero", "small"),
    ("The logo is a bit too big on mobile", "small"),
    ("Make the button blue", "small"),
    ("Change the headline to 'Your trades remember.'", "small"),
    ("Add a period at the end of the tagline", "small"),
    ("Tweak the footer spacing", "small"),
    ("The CTA text is cut off on small screens", "small"),
    ("Swap the hero image for the new screenshot", "small"),
    ("Bump the font size on the pricing cards", "small"),
    ("Center the logo in the nav", "small"),
    ("Replace 'sign up' with 'join the waitlist'", "small"),
    ("There's a typo in the FAQ", "small"),
    ("Make the links underline on hover", "small"),
    ("The contact form doesn't submit, can you look?", "small"),
    ("Update the copyright year in the footer", "small"),
    ("Nudge the logo a few pixels left", "small"),
    ("Remove the extra comma in the second paragraph", "small"),
    ("Dark mode colours are off on the pricing page", "small"),
    ("Design the onboarding flow", "new"),
    ("Let's work out the monetization", "new"),
    ("Plan the pricing tiers", "new"),
    ("Sketch the main screens for the app", "new"),
    ("Write the product vision doc", "new"),
    ("Figure out how we charge for the API", "new"),
    ("We need a paywall", "new"),
    ("Come up with the subscription model", "new"),
    ("Rename the Pro tier to Plus", "small"),
    ("Change the annual price to $49", "small"),
    ("The paywall close button is too small", "small"),
    ("Reword the second bullet in the vision", "small"),
    ("Make the onboarding skip button more visible", "small"),
    ("Adjust the trial length from 7 to 14 days", "small"),
    ("Fix the spelling of 'lifetime' on the paywall", "small"),
    ("Build the settings screen", "new"),
    ("Implement the journal API", "new"),
    ("Create the trades table and its API", "new"),
    ("Add a search screen", "new"),
    ("Write the endpoints for reflections", "new"),
    ("I want an export feature", "new"),
    ("Make a dashboard screen showing weekly stats", "new"),
    ("Build out the notifications system", "new"),
    ("The settings screen crashes when you tap back", "small"),
    ("Return 404 instead of 500 when the trade is missing", "small"),
    ("Add a null check in the journal endpoint", "small"),
    ("Rename the field createdAt to created_at", "small"),
    ("The list doesn't scroll to the bottom", "small"),
    ("Make the header sticky", "small"),
    ("Sort the trades by date descending", "small"),
    ("Increase the API timeout to 30 seconds", "small"),
    ("Log the request id on errors", "small"),
    ("Write the script for the launch video", "new"),
    ("Storyboard the 30 second ad", "new"),
    ("Cut the launch video", "new"),
    ("Make a teaser clip for the waitlist", "new"),
    ("Trim the first two seconds of the intro", "small"),
    ("The music is too loud in the edit", "small"),
    ("Change the end card to show the URL", "small"),
    ("Fix the subtitle timing at 0:12", "small"),
    ("Write the README", "new"),
    ("Draft the architecture doc", "new"),
    ("Write the launch blog post", "new"),
    ("Fix the broken link in the README", "small"),
    ("Update the install command in the docs", "small"),
    ("Reword the intro paragraph of the blog post", "small"),
)

FEW_SHOT: tuple[tuple[str, str], ...] = (
    ("Make the button blue", "B"),
    ("Add a pricing page", "A"),
    ("Add a period at the end of the tagline", "B"),
    ("Let's do the landing page now", "A"),
    ("The CTA text is cut off on small screens", "B"),
    ("Rebuild the homepage with the new layout", "A"),
    ("Rename the Pro tier to Plus", "B"),
    ("We need a paywall", "A"),
)


SIZE_QUESTION = Question(
    name="task_size",
    instruction=LETTER_PROMPT,
    few_shot=FEW_SHOT,
    examples=tuple((task, label == "new") for task, label in EXAMPLES),
)

# Kept for callers written against the task sizer.
SizerUnavailable = ClassifierUnavailable
_gpu_memory_mib = gpu_memory_mib
_p_first_letter = p_first_letter


def letter_messages(task: str, *, few_shot: bool) -> list[dict[str, str]]:
    """The chat for one request: the instruction, examples if asked, the request."""
    return SIZE_QUESTION.messages(task, few_shot=few_shot)


class ModelSizer(LocalModel):
    """A small local model through Ollama, asked for one letter: new (A) or small (B)."""

    def __init__(
        self,
        model: str,
        *,
        url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 10.0,
        vram_headroom: float = 0.3,
        gpu_memory: Callable[[], tuple[int, int] | None] = gpu_memory_mib,
        few_shot: bool = True,
        when_cold: str = "load",
        keep_alive: str = KEEP_ALIVE,
    ) -> None:
        super().__init__(
            model, url=url, timeout_seconds=timeout_seconds,
            vram_headroom=vram_headroom, gpu_memory=gpu_memory, when_cold=when_cold,
            keep_alive=keep_alive,
        )
        # Zero-shot scored 66.7% against 98.6% with the examples; kept for the bench.
        self.few_shot = few_shot

    def __call__(self, task: str, steps: Sequence[Step]) -> Sizing:
        return Sizing("new" if self.p_new(task) >= 0.5 else "small", "model")

    def p_new(self, task: str) -> float:
        return self.p_a(SIZE_QUESTION, task, few_shot=self.few_shot)


class EmbeddingSizer:
    """The archive's own sentence embedding, nearest of two authored centroids."""

    def __init__(self, embed: Callable[[list[str]], list[list[float]]]) -> None:
        self._centroids = Centroids(embed, SIZE_QUESTION.examples)

    def __call__(self, task: str, steps: Sequence[Step]) -> Sizing:
        return Sizing("new" if self._centroids.is_a(task) else "small", "embedding")


REDO_PROMPT = (
    "You read one request made to a coding or design agent about a step of work "
    "(a logo, a page, a data model, a script, a draft). Answer A if it redoes that "
    "step: it remakes or substantially reworks something already made (start over, "
    "a different direction, a replacement, rethinking it). Answer B if it does not: "
    "it makes the thing for the first time, carries on with it, makes a small change "
    "to it, or only repeats an action such as running, checking or exporting. Answer "
    "with the single letter A or B."
)

# Authored examples, the dev set of scripts/fixtures/redo_dev.json and nothing
# else. Not from any owner's archive.
REDO_EXAMPLES: tuple[tuple[str, bool], ...] = (
    ("redo the logo, the current one feels generic", True),
    ("lets start the landing page over with a simpler layout", True),
    ("the colours dont work together, pick a whole new palette", True),
    ("rebuild the page content around the new vision", True),
    ("throw away the hero and do it from scratch", True),
    ("the logo needs a completely different direction", True),
    ("rewrite all the page copy, its too salesy", True),
    ("make the logo", False),
    ("fix the typo in the footer", False),
    ("choose the colours for the site", False),
    ("nudge the logo a few pixels left", False),
    ("write the page content for the about section", False),
    ("check the colours again on mobile", False),
    ("make the link colour a bit darker", False),
    ("rethink the vision, it no longer matches what we built", True),
    ("redesign the whole onboarding flow", True),
    ("scrap the monetization plan and start over", True),
    ("the design doesnt fit the new direction, rework it", True),
    ("write the vision", False),
    ("add a line about teams to the vision", False),
    ("sketch the first design of the settings screen", False),
    ("change the monthly price to 12", False),
    ("run the pricing numbers again with the new costs", False),
    ("redo the data model, the relations are wrong throughout", True),
    ("rewrite the api in fastapi instead", True),
    ("the screens need another pass from the top, they feel dated", True),
    ("replace the whole data model with an event log", True),
    ("design the data model", False),
    ("add a created_at column", False),
    ("build the login screen", False),
    ("rename the users endpoint", False),
    ("run the api tests again", False),
    ("continue with the screens", False),
    ("rewrite the script, the story doesnt land", True),
    ("redo the storyboard with fewer scenes", True),
    ("re-edit the whole thing to be half as long", True),
    ("write the script for the teaser", False),
    ("trim two seconds from the intro", False),
    ("storyboard the second half", False),
    ("export the edit again at 1080p", False),
    ("restructure the outline from scratch", True),
    ("the draft is off, write it again from a user's view", True),
    ("overhaul the publish setup, the current one is a mess", True),
    ("outline the api guide", False),
    ("fix the broken link in the draft", False),
    ("publish the draft", False),
    ("add a faq to the draft", False),
    ("read the draft again and fix typos", False),
)

REDO_FEW_SHOT: tuple[tuple[str, str], ...] = (
    ("make the logo", "B"),
    ("redo the logo, the current one feels generic", "A"),
    ("run the api tests again", "B"),
    ("the design doesnt fit the new direction, rework it", "A"),
    ("nudge the logo a few pixels left", "B"),
    ("rewrite the api in fastapi instead", "A"),
    ("export the edit again at 1080p", "B"),
    ("the screens need another pass from the top, they feel dated", "A"),
)

REDO_QUESTION = Question(
    name="redo", instruction=REDO_PROMPT, few_shot=REDO_FEW_SHOT, examples=REDO_EXAMPLES
)


class ChainSizer:
    """Each sizer in turn; the first that answers decides. The lexicon always does.

    ``redo`` is the chain that decides whether a task redoes a step; without one,
    the verb list in ``workflows.is_redo`` does.
    """

    def __init__(
        self, sizers: Sequence[TaskSizer], *, redo: BinaryClassifier | None = None
    ) -> None:
        self.sizers = tuple(sizers)
        self.redo = redo
        self.skipped: list[str] = []

    def __call__(self, task: str, steps: Sequence[Step]) -> Sizing:
        for sizer in self.sizers:
            try:
                return sizer(task, steps)
            except ClassifierUnavailable as exc:
                self.skipped.append(str(exc))
        return lexicon_sizer(task, steps)

    def is_redo(self, task: str, steps: Sequence[Step]) -> bool:
        verdict = None if self.redo is None else self.redo(task)
        if verdict is None or verdict.by == "rule":
            # The verb list reads the task without the workflow's own step names.
            return is_redo(task, steps)
        return verdict.is_a


def redo_classifier_from_config(
    cfg: Any,
    *,
    model: LocalModel | None = None,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
) -> BinaryClassifier | None:
    """`[workflows] redo_classifier`: the chain for rework, or None for the verb list."""
    mode = cfg.workflows.redo_classifier
    if mode == "rule":
        return None
    if mode == "model":
        # Model, then the verb list: the embedding scored no better than the verb
        # list held out (75.0% vs 73.1%) and calls more small changes rework.
        return BinaryClassifier(
            REDO_QUESTION, lambda task: is_redo(task), model=model or local_model_from_config(cfg)
        )
    return classifier_from_config(
        cfg, REDO_QUESTION, lambda task: is_redo(task), mode, model=model, embed=embed
    )


def sizer_from_config(cfg: Any) -> ChainSizer:
    """The sizers `[workflows] task_classifier` asks for, best first, and the rework chain."""
    settings = cfg.workflows
    modes = (settings.task_classifier, settings.redo_classifier)
    # One model client and one embedding for both chains: the embedding loads once.
    model = local_model_from_config(cfg) if "model" in modes else None
    embed = (
        embedder_from_config(cfg)
        if settings.task_classifier in ("model", "embedding")
        or settings.redo_classifier == "embedding"
        else None
    )
    sizers: list[TaskSizer] = []
    if settings.task_classifier == "model" and settings.inference_mode != "conversations":
        sizers.append(ModelSizer(
            settings.classifier_model,
            url=settings.classifier_url,
            timeout_seconds=settings.classifier_timeout_seconds,
            vram_headroom=settings.classifier_vram_headroom,
            keep_alive=settings.classifier_keep_alive,
        ))
    if settings.task_classifier in ("model", "embedding") and embed is not None:
        sizers.append(EmbeddingSizer(embed))
    return ChainSizer(sizers, redo=redo_classifier_from_config(cfg, model=model, embed=embed))


def gate_sizer_from_config(cfg: Any) -> ModelSizer | None:
    """The sizer the finish check trusts to let a small change through: the model
    alone, and only while it is loaded. On 42 real tasks it let no new task
    through; the embedding let 2 through and the lexicon 1, so they never skip it.
    """
    settings = cfg.workflows
    if settings.task_classifier != "model" or settings.inference_mode == "conversations":
        return None
    return ModelSizer(
        settings.classifier_model,
        url=settings.classifier_url,
        timeout_seconds=settings.classifier_timeout_seconds,
        vram_headroom=settings.classifier_vram_headroom,
        when_cold="skip",
        keep_alive=settings.classifier_keep_alive,
    )


def classifier_status(
    cfg: Any,
    *,
    gpu_memory: Callable[[], tuple[int, int] | None] = gpu_memory_mib,
) -> tuple[bool, str]:
    """Which sizer answers now, for `varyz hooks doctor`. It probes; it loads nothing.

    Always ok: Ollama is optional and a fallback always answers. The detail says
    which one, and what to do when it is not the configured one.
    """
    settings = cfg.workflows
    if settings.task_classifier == "lexicon":
        return True, "verb list, as configured (60.9% held out)"
    if settings.task_classifier == "embedding":
        return True, "local embedding, as configured (89.9% held out)"
    sizer = local_model_from_config(cfg, gpu_memory=gpu_memory)
    try:
        sizer.ready()
    except ClassifierUnavailable as exc:
        return True, (
            f"local embedding answers for now (89.9% vs 98.6%): {exc}. "
            f"Start Ollama and `ollama pull {sizer.model}`, or set "
            "[workflows] task_classifier = \"embedding\""
        )
    return True, f"{sizer.model} through Ollama on this machine (98.6% held out)"


__all__ = [
    "EXAMPLES",
    "FEW_SHOT",
    "KEEP_ALIVE",
    "LETTER_PROMPT",
    "REDO_QUESTION",
    "SIZE_QUESTION",
    "ChainSizer",
    "EmbeddingSizer",
    "ModelSizer",
    "SizerUnavailable",
    "classifier_status",
    "gate_sizer_from_config",
    "letter_messages",
    "loaded_mib",
    "redo_classifier_from_config",
    "sizer_from_config",
]
