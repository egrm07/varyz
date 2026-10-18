"""Is an owner answer how they work everywhere, or a choice for one product?

"Always self-host fonts" is a preference: it should shape every project. "Use
#6d5efc for the accent" is a one-off product decision: it must never leak into
the next project. The owner is the authority on which one they meant, so the
agent offers both readings in its question and the owner's pick is stored.
The default is chosen only when the owner's words did not say, and it fails
narrow: an unclear answer binds to the project it was given in, because a leaked
product choice is worse than asking again elsewhere. A question plainly about
this product or the task in hand is not put to the owner as "all projects or
just this one?" at all: that follow-up is noise. When the local model is
configured, it reads the exchange and must be sure (P(general) at least
``GENERAL_THRESHOLD``) before anything is called general; otherwise, or when it
cannot answer, the wording heuristics below decide.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from varyz.resolution.knowledge import GLOBAL_SCOPE, normalize_scope
from varyz.resolution.local_classifier import ClassifierUnavailable, LocalModel, Question

SCOPE_PROMPT = (
    "An agent asked its owner a question while working on one of the owner's "
    "projects, and the owner answered. Decide how far the answer reaches. Answer A "
    "if it is how the owner works everywhere: a habit, a taste, a quality bar or a "
    "tool they would want in every project. Answer B if it is a choice for this "
    "product only: a concrete value such as a colour, a name, copy, a price, a date, "
    "a number or what this product includes. When unsure, answer B. Answer with the "
    "single letter A or B."
)

# Authored examples from the dev pairs of scripts/fixtures/workflow_bench.json and
# nothing else. Not from any owner's archive.
SCOPE_FEW_SHOT: tuple[tuple[str, str], ...] = (
    ("Asked: Which accent colour for the landing page? | Answered: #6d5efc", "B"),
    ("Asked: Do you want a dry-run mode for this tool? | Answered: I always want a "
     "dry-run first.", "A"),
    ("Asked: What price for the Pro tier? | Answered: $9.99 a month.", "B"),
    ("Asked: How should secrets be stored? | Answered: Windows Credential Manager or an "
     "untracked .env, never in code.", "A"),
    ("Asked: Dark or light hero section? | Answered: Dark for this launch.", "B"),
    ("Asked: Should reports lead with numbers? | Answered: I prefer numbers first, "
     "failures included.", "A"),
    ("Asked: How many pricing tiers on this page? | Answered: Three.", "B"),
    ("Asked: Should I add social share cards? | Answered: Generally yes, Open Graph and "
     "Twitter cards.", "A"),
)

SCOPE_QUESTION = Question(
    name="scope", instruction=SCOPE_PROMPT, few_shot=SCOPE_FEW_SHOT, input_label="Exchange"
)
# The model must be this sure before an answer is stored for every project.
GENERAL_THRESHOLD = 0.9


def scope_exchange(question: str, answer: str) -> str:
    return f"Asked: {' '.join(question.split())} | Answered: {' '.join(answer.split())}"

# The owner said outright that it applies beyond this project.
_EXPLICIT_GENERAL = re.compile(
    r"\b(?:all|every|any|each)\s+(?:of\s+)?(?:my\s+|the\s+)?"
    r"(?:projects?|products?|apps?|applications?|sites?|websites?|tools?|repos?|"
    r"repositories|codebases?|pages?|releases?|clis?)\b"
    r"|\bin general\b|\beverywhere\b|\bacross (?:all|every|the board)\b"
    r"|\bby default\b|\bas a rule\b|\bgoing forward\b|\bfrom now on\b"
    r"|\bmy (?:projects|products|apps|websites|sites|tools|repos)\b"
)
# Habitual wording: a standing way of working rather than a one-time pick.
_HABITUAL = re.compile(
    r"\b(?:always|never|whenever|every time|each time|usually|generally|"
    r"i (?:always|never|prefer|like|hate|want every|expect)|i'd rather|i would rather|"
    r"make sure|don't ever|do not ever)\b"
)
# Pointing at the thing in front of the agent.
_DEIXIS = re.compile(
    r"\b(?:this|the|our)\s+(?:one|page|landing(?: page)?|site|website|app|application|"
    r"project|product|repo|repository|tool|screen|brand|logo|launch|release|campaign|"
    r"build|version|deck|ui|hero|header|footer|button|section)\b"
    # The task in hand: "this final alpha test-and-fix pass", "this change".
    r"|\b(?:this|our)\s+(?:[\w-]+\s+){0,3}(?:task|change|pass|pr|pull request|branch|"
    r"commit|fix|bug|feature|refactor|migration|milestone|sprint|alpha|beta|draft|run|"
    r"module|file|function|test|endpoint|command|step)s?\b"
    r"|\bhere\b|\bfor now\b|\bthis time\b|\bfor this\b|\bright now\b"
)
# Literal product values: colours, exact names, copy, prices, versions.
_LITERAL = re.compile(
    r"#[0-9a-f]{3,8}\b|\brgba?\(|\bhsl\(|\b\d+(?:\.\d+)?\s*(?:px|rem|em|%)"
    r"|[\"'“‘][^\"'”’]{2,60}[\"'”’]"
    r"|\b(?:tagline|slogan|wordmark|logo|palette|accent|brand colou?r|primary colou?r|"
    r"headline|hero copy|domain name|price|pricing|called|named|name it|call it)\b"
    r"|[$€£]\s?\d"
    r"|\bv?\d+\.\d+(?:\.\d+)?\b"
)


@dataclass(frozen=True)
class ScopeSuggestion:
    scope: str  # '*' or the project name
    reason: str
    confident: bool

    @property
    def general(self) -> bool:
        return self.scope == GLOBAL_SCOPE

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": "general" if self.general else "project",
            "project": None if self.general else self.scope,
            "reason": self.reason,
            "confident": self.confident,
        }


def _fold(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def suggest_scope(
    question: str,
    answer: str,
    project: str | None,
    *,
    model: LocalModel | None = None,
    threshold: float = GENERAL_THRESHOLD,
) -> ScopeSuggestion:
    """Default scope for an answer the owner did not scope explicitly.

    Explicit wording ("in all my projects", naming the project) always decides.
    Otherwise ``model``, when given and able to answer, decides; then the
    wording heuristics.
    """
    project_scope = normalize_scope(project)
    if project_scope == GLOBAL_SCOPE:
        return ScopeSuggestion(GLOBAL_SCOPE, "no project in play", confident=False)
    q, a = _fold(question), _fold(answer)
    both = f"{q} \n {a}"
    if _EXPLICIT_GENERAL.search(a):
        return ScopeSuggestion(GLOBAL_SCOPE, "the answer says it applies generally", True)
    names_project = re.search(
        rf"(?<![\w-]){re.escape(project_scope)}(?![\w-])", both
    ) is not None
    literal = _LITERAL.search(a) is not None or _LITERAL.search(q) is not None
    deictic = _DEIXIS.search(both) is not None
    habitual = _HABITUAL.search(a) is not None or _HABITUAL.search(q) is not None
    if names_project:
        return ScopeSuggestion(project_scope, f"names {project_scope}", True)
    if model is not None:
        try:
            p = model.p_a(SCOPE_QUESTION, scope_exchange(question, answer))
        except ClassifierUnavailable:
            pass
        else:
            if p >= threshold:
                return ScopeSuggestion(GLOBAL_SCOPE, "reads as a habit (local model)", True)
            return ScopeSuggestion(
                project_scope,
                "reads as a choice for this project (local model)"
                if p < 1 - threshold
                else "unclear; kept to this project (fail narrow)",
                p < 1 - threshold,
            )
    if habitual and not literal:
        return ScopeSuggestion(GLOBAL_SCOPE, "habitual wording, no product-specific value", True)
    if literal:
        return ScopeSuggestion(project_scope, "a product-specific value", not habitual)
    if deictic:
        return ScopeSuggestion(project_scope, "refers to this project", True)
    return ScopeSuggestion(project_scope, "unclear; kept to this project (fail narrow)", False)


def scope_options(
    question: str,
    answer_hint: str | None,
    project: str | None,
    *,
    model: LocalModel | None = None,
) -> dict[str, object]:
    """How the agent should let the owner choose, in the question it asks them."""
    project_scope = normalize_scope(project)
    if project_scope == GLOBAL_SCOPE:
        return {
            "project": None,
            "how": "No project is known, so the answer is stored as a general preference.",
        }
    suggestion = suggest_scope(question, answer_hint or "", project, model=model)
    if not suggestion.general and suggestion.confident:
        # Plainly about this product or the task in hand: asking its reach is noise.
        return {
            "project": project_scope,
            "default": "project",
            "ask": False,
            "how": (
                f"This is a choice for {project_scope} ({suggestion.reason}). Do not ask "
                "the owner whether it applies to all projects; pass scope='project' to "
                "answer_owner_question."
            ),
        }
    return {
        "project": project_scope,
        "default": "general" if suggestion.general else "project",
        "ask": True,
        "how": (
            "Offer the owner's answer in two forms so they also decide its reach: "
            f"'Always, in all my projects: …' and 'Just for {project_scope}: …'. Pass "
            "scope='general' or scope='project' to answer_owner_question to match what "
            "they picked. A taste or habit (how they build, quality bars, tooling) is "
            "general; a concrete value for this product (a colour, name, copy, price) is "
            "project. If they type their own answer and it is about the work in hand, "
            "pass scope='project' without asking them again."
        ),
    }


def parse_scope_choice(choice: str | None, project: str | None) -> str | None:
    """Map an agent's 'general' | 'project' | explicit name onto a stored scope."""
    if choice is None or not choice.strip():
        return None
    value = _fold(choice)
    # Agents often pass back the option label the owner picked, not the keyword.
    label = value.split(":", 1)[0].strip()
    always_option = re.fullmatch(r"always(?:,\s*|\s+)(?:in\s+)?(.+)", label)
    if normalize_scope(value) == GLOBAL_SCOPE or label == "always" or (
        ":" in value and normalize_scope(label) == GLOBAL_SCOPE
    ) or (always_option and normalize_scope(always_option[1]) == GLOBAL_SCOPE):
        return GLOBAL_SCOPE
    project_label = " ".join(re.split(r"[\s_-]+", value))
    if project_label in {"project", "this project", "local", "just this project", "only here",
                         "current project", "this project only", "project only"} or (
        re.match(r"(?:just|only) (?:for|in|here|this)\b", label)
    ):
        scope = normalize_scope(project)
        if scope == GLOBAL_SCOPE:
            raise ValueError("scope='project' needs a known project")
        return scope
    return normalize_scope(value)


def scope_model_from_config(cfg: Any) -> LocalModel | None:
    """`[workflows] scope_classifier`: the local model, or None for the heuristics."""
    if cfg.workflows.scope_classifier != "model":
        return None
    from varyz.resolution.local_classifier import local_model_from_config

    return local_model_from_config(cfg)


__all__ = [
    "GENERAL_THRESHOLD",
    "SCOPE_QUESTION",
    "ScopeSuggestion",
    "parse_scope_choice",
    "scope_model_from_config",
    "scope_options",
    "suggest_scope",
]
