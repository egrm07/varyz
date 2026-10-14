"""Does a prompt ask for work, or only for a reply?

The briefing hook hands the agent the owner's definition of done only with work.
The wording rule in ``varyz.hooks.is_question`` calls a prompt a question when
every sentence asks something. It misses a reply-only request worded as an
instruction ("check whether the backup finished and tell me", "give me five
name ideas") and a request worded as a question it has no pattern for.
This is the same
local A/B decision as the task sizer (``varyz.resolution.local_classifier``),
asked "work or reply?", measured on authored held-out prompts and a labelled
sample of real ones (reports/LOCAL_CLASSIFIER_ROLLOUT_2026-09-25.md).
"""

from __future__ import annotations

import contextlib
from typing import Any

from varyz.resolution.local_classifier import (
    BinaryClassifier,
    ClassifierUnavailable,
    LocalModel,
    Question,
    classifier_from_config,
    local_model_from_config,
)

WORK_PROMPT = (
    "You read one message a person sent to their coding or design agent. Answer A "
    "if it asks the agent to do work: change files, systems or state (make, edit, "
    "fix, delete, run, launch, or carry on with work already started). A request "
    "phrased as a question is work, and so is a bug report or a complaint about "
    "something the agent built. Answer B if it only wants a reply in chat: an "
    "answer, an explanation, advice, ideas, a status report, or checking something "
    "and telling them. Answer with the single letter A or B."
)

# Authored examples, the dev set of scripts/fixtures/work_question_dev.json and
# nothing else. Not from any owner's archive.
WORK_EXAMPLES: tuple[tuple[str, bool], ...] = (
    ('add a dark mode toggle to the settings screen', True),
    ('can you wire the export button to the csv endpoint', True),
    ('the signup form throws on empty email, fix it', True),
    ('write a readme for the cli with install steps', True),
    ('rename the config key from api_url to base_url everywhere', True),
    ('kick off the nightly import and let it run', True),
    ('delete the old screenshots folder, we dont need it', True),
    ('carry on with the migration where you left off', True),
    ('could you make the hero image load lazily', True),
    ('the chart labels overlap on small screens', True),
    ('put the pricing tiers into a table on the pricing page', True),
    ('i want the app to remember the last opened tab', True),
    ('set up a github action that runs the tests on push', True),
    ('go with the second option and build it', True),
    ('update the changelog for the 0.4 release', True),
    ('please bump the timeout to 30 seconds', True),
    ('draft the launch email and save it in docs', True),
    ('the logo is too big on mobile, shrink it', True),
    ('stop the training run and free the gpu', True),
    ('would you add retries to the webhook sender', True),
    ('make a script that backs up the database every night', True),
    ('ok do that, and update the todo too', True),
    ('i dont like the gradient on the buttons, make them flat', True),
    ('profile the search endpoint and speed it up', True),
    ('merge the two settings pages into one', True),
    ('the scheduler should skip weekends from now on', True),
    ('install the pre-commit hooks and run them once', True),
    ('yes, go ahead and remove the unused dependencies', True),
    ('translate the onboarding copy into spanish', True),
    ('resume the compile, it crashed overnight', True),
    ('what does the sync cursor do', False),
    ('why did the build fail this morning', False),
    ('how long will the backfill take', False),
    ('is the api rate limited', False),
    ('which of these two logos do you prefer', False),
    ('should we use postgres or sqlite for this', False),
    ('where are we with the release', False),
    ('explain how the ranking works in simple words', False),
    ('give me five name ideas for the newsletter', False),
    ('tell me what you changed yesterday', False),
    ('check whether the backup finished and tell me', False),
    ('do you think the pricing is too low', False),
    ('what would it cost to host this on fly', False),
    ('i wonder if the cache is even helping', False),
    ('has the gpu job finished yet', False),
    ('is there a better way to store the tokens', False),
    ('none of those names work for me, any others?', False),
    ('compare react native and flutter for this app', False),
    ('what is left before we can launch', False),
    ('how much disk does the vault use now', False),
    ('would a local model be good enough for this', False),
    ('summarise what the codex review found', False),
    ('so is this actually useful to anyone', False),
    ('which step would you do next and why', False),
    ('i noticed the dashboard loads slowly on monday mornings, any idea why?', False),
    ('what do you recommend for the accent colour', False),
    ('how does this compare to what we had before', False),
    ('can you explain what the watchdog does', False),
    ('in percent, how close are we to done', False),
    ('does the importer handle deleted messages', False),
)

WORK_FEW_SHOT: tuple[tuple[str, str], ...] = (
    ("add a dark mode toggle to the settings screen", "A"),
    ("check whether the backup finished and tell me", "B"),
    ("can you wire the export button to the csv endpoint", "A"),
    ("what does the sync cursor do", "B"),
    ("the chart labels overlap on small screens", "A"),
    ("give me five name ideas for the newsletter", "B"),
    ("ok do that, and update the todo too", "A"),
    ("i wonder if the cache is even helping", "B"),
)

WORK_QUESTION = Question(
    name="work_or_question",
    instruction=WORK_PROMPT,
    few_shot=WORK_FEW_SHOT,
    examples=WORK_EXAMPLES,
    input_label="Message",
)


def prompt_classifier_from_config(
    cfg: Any, *, model: LocalModel | None = None
) -> BinaryClassifier | None:
    """`[workflows] prompt_classifier`: the chain for work vs reply, or None for the rule.

    It runs in the prompt hook, before every prompt, so ``model`` goes straight
    to the wording rule: a cold model is loaded in the background for the next
    prompt rather than waited for (4 s), and the embedding is skipped (2 s to
    load in a hook process, no better than the rule on real prompts).
    """
    mode = cfg.workflows.prompt_classifier
    if mode == "rule":
        return None
    from varyz.hooks import is_question

    def rule(text: str) -> bool:
        return not is_question(text)

    if mode == "model":
        return BinaryClassifier(
            WORK_QUESTION, rule,
            model=model or local_model_from_config(cfg, when_cold="warm"),
        )
    return classifier_from_config(cfg, WORK_QUESTION, rule, mode)


def warm_for_session(cfg: Any) -> None:
    """Start loading the model at session start, so the first prompt finds it warm."""
    if cfg.workflows.prompt_classifier != "model":
        return
    with contextlib.suppress(ClassifierUnavailable):
        local_model_from_config(cfg, when_cold="warm").ready()


__all__ = [
    "WORK_EXAMPLES",
    "WORK_FEW_SHOT",
    "WORK_QUESTION",
    "prompt_classifier_from_config",
    "warm_for_session",
]
