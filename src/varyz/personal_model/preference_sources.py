"""Conservative first-person preference spans from owner-authored AI turns.

These are source spans, not claims about a global personality. Quoted examples,
code, and one-off instructions do not become preferences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MARKER = re.compile(
    r"\bI\s+(?:really\s+)?(?P<verb>don't\s+like|dont\s+like|do\s+not\s+like|"
    r"prefer|like|love|hate|dislike)\s+",
    re.IGNORECASE,
)
_END = re.compile(r"(?:,\s+but\b|\s+but\b|[.!?;\r\n])", re.IGNORECASE)
_VAGUE = frozenset({"it", "this", "that", "them", "something", "anything"})
_CONTEXTUAL_APPROVAL = re.compile(
    r"^(?:(?:your|this|that)\s+(?:suggestion|idea|version|answer)|"
    r"how\s+(?:this|the)\s+(?:ai|model)\s+responds)\b",
    re.IGNORECASE,
)
_DIRECTIVE = re.compile(
    r"(?:^|(?<=[.!?,])\s+)(?P<verb>make|create|simplify|change|have|keep|use|remove|avoid|no|"
    r"don't|do\s+not|I\s+want|I\s+don't\s+want|I\s+do\s+not\s+want)\s+",
    re.IGNORECASE,
)
_VISUAL = re.compile(
    r"\b(background|buttons?|layout|ui|colou?rs?|style|fonts?|glassy|clean|simple|"
    r"contrast|palette|animations?|borders?|boxes?|box|visual|design|website|screen)\b",
    re.IGNORECASE,
)
_WORKFLOW = re.compile(
    r"\b(weekly\s+evaluations?|autonom(?:ous|ously)|workflow|approvals?|"
    r"notifications?|schedule|without me|oauth|api\s+keys?|openrouter|"
    r"manual\s+exports?|fetching|gpu|cloud\s+model|local\s+model)\b",
    re.IGNORECASE,
)
_COMMUNICATION_DIRECTIVE = re.compile(
    r"\b(?:(?:I\s+want\s+you\s+to\s+)?remember\s+to\s+(?:always\s+)?"
    r"(?:report|give|keep)\b|"
    r"(?:I\s+want\s+you\s+to\s+)?keep\s+(?:reports?|summaries)\b|"
    r"(?:I\s+want\s+you\s+to\s+)?type\s+(?:way\s+)?more\s+concise\b|"
    r"(?:do\s+not|don't|dont|never)\s+use\s+emojis\b|"
    r"I\s+need\s+(?:a\s+)?shorter\s+summary\b)",
    re.IGNORECASE,
)
_COMMUNICATION_STYLE = re.compile(
    r"\b(concise|brief|shorter|less\s+random\s+numbers|more\s+conclusions|"
    r"no\s+emojis|use\s+emojis)\b",
    re.IGNORECASE,
)
_REUSABLE_STYLE = re.compile(
    r"\b(clean(?:er)?|simpl(?:e|er|ify)|integrat(?:ed|e)|cohesive|smooth(?:er)?|"
    r"contrast|glassy|dark|light|bright|purple|blue|pink|red|white|"
    r"palette|colou?r|circular|rounded|warm|minimal|innovative|professional|"
    r"human|pretty|apple\s+style|empty\s+space)\b|#[0-9a-fA-F]{6}\b",
    re.IGNORECASE,
)
_LOCAL_ELEMENT = re.compile(
    r"(?:(?:the|this|that|a|an)\s+)?(?:\(-\)\s+)?(?:(?:whole|current|existing|new|ai|analytics|"
    r"restore|skip|subscription|download|pin|journal)\s+){0,3}"
    r"(?:buttons?|box|boxes|cards?|icons?|fonts?|screens?|pin\s+pad|text\s+box)\b",
    re.IGNORECASE,
)
_BROAD_TARGET = re.compile(
    r"\b(all|every|overall|throughout|across|whole\s+(?:app|site|ui))\b",
    re.IGNORECASE,
)
_SUBPART_COLOUR = re.compile(r"\bcolou?r\s+of\b", re.IGNORECASE)
_IMPLEMENTATION_DETAIL = re.compile(
    r"\b(keyboard\s+popup|number\s+buttons|entering\s+screen|"
    r"tell\s+me\s+what\s+colou?r|notification\s+preferences\s+out|"
    r"follow\s+exactly\s+what\s+the\s+website\s+has)\b",
    re.IGNORECASE,
)
_PROJECT_ANCHOR = re.compile(
    r"\b(?:my\s+project|project)\s+(?P<project>persona|varyz|varyz|san7|memoir|marketing)\b",
    re.IGNORECASE,
)
_DESIGN_RESET = re.compile(r"\b(?:abandon|scrap|start\s+over)\b.{0,40}\bdesign\b", re.I)


@dataclass(frozen=True)
class PreferencePhrase:
    quote: str
    object_literal: str
    valence: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ProjectGuidancePhrase:
    quote: str
    object_literal: str
    dimension: str
    valence: str
    char_start: int
    char_end: int


def explicit_preference_phrases(body: str) -> tuple[PreferencePhrase, ...]:
    """Return only simple owner-voiced assertions with exact offsets in ``body``."""
    separator = body.find("\n\n")
    content_start = separator + 2 if body.startswith("From:") and separator >= 0 else 0
    result: list[PreferencePhrase] = []
    in_fence = False
    offset = content_start
    for line in body[content_start:].splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            offset += len(line)
            continue
        if in_fence or stripped.startswith((">", "{", "[", "+", "@@")):
            offset += len(line)
            continue
        for match in _MARKER.finditer(line):
            prefix = line[: match.start()]
            if prefix.count('"') % 2 or prefix.count("'") % 2:
                continue
            if prefix.strip() and not (
                len(prefix) <= 80 and re.search(r"[.!?,]\s*$", prefix)
            ):
                continue
            remainder = line[match.end() :]
            ending = _END.search(remainder)
            object_literal = (remainder[: ending.start()] if ending else remainder).strip()
            if not 3 <= len(object_literal) <= 160 or object_literal.casefold() in _VAGUE:
                continue
            if _CONTEXTUAL_APPROVAL.search(object_literal):
                continue
            quote_end = match.end() + len(object_literal)
            quote = line[match.start() : quote_end]
            verb = re.sub(r"\s+", " ", match.group("verb").casefold())
            valence = (
                "dislike"
                if verb in {"don't like", "dont like", "do not like", "hate", "dislike"}
                else "prefer"
                if verb == "prefer"
                else "like"
            )
            result.append(
                PreferencePhrase(
                    quote=quote,
                    object_literal=object_literal,
                    valence=valence,
                    char_start=offset + match.start(),
                    char_end=offset + quote_end,
                )
            )
        offset += len(line)
    return tuple(result)


def ai_project_scope(body: str) -> str | None:
    """Preserve the connector's project label; never promote it to global."""
    header = body.split("\n\n", 1)[0]
    match = re.search(r"(?m)^Conversation: .+? [—-] (.+)$", header)
    if match is None:
        return None
    label = match.group(1).strip()[:160]
    if re.fullmatch(r"(?i)SAN7(?:\.\d+)?", label):
        return "SAN7"
    if label.casefold() in {"codex", "website", "i", "control-my-computer-and-click-win"}:
        # These are working-directory names, not trustworthy product identities.
        return None
    return label


def explicit_thread_project_anchor(body: str) -> str | None:
    """A generic AI working folder may inherit only an explicit first-turn project."""
    content = body.split("\n\n", 1)[-1]
    match = _PROJECT_ANCHOR.search(content[:500])
    if match is None:
        return None
    project = match.group("project")
    return "SAN7" if project.casefold() == "san7" else project.casefold()


def resets_design_direction(body: str) -> bool:
    """Recognize an owner explicitly retiring the previous design in a thread."""
    content = body.split("\n\n", 1)[-1]
    return bool(_DESIGN_RESET.search(content[:500]))


def project_guidance_phrases(body: str) -> tuple[ProjectGuidancePhrase, ...]:
    """Find concrete project-specific visual/workflow directions, not general taste."""
    separator = body.find("\n\n")
    content_start = separator + 2 if body.startswith("From:") and separator >= 0 else 0
    result: list[ProjectGuidancePhrase] = []
    in_fence = False
    offset = content_start
    for line in body[content_start:].splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            offset += len(line)
            continue
        if in_fence or stripped.startswith((">", "{", "[", "+", "@@")):
            offset += len(line)
            continue
        covered_until = 0
        for match in _DIRECTIVE.finditer(line):
            if match.start("verb") < covered_until:
                continue
            remainder = line[match.end() :]
            ending = _END.search(remainder)
            object_literal = (remainder[: ending.start()] if ending else remainder).strip()
            if not 8 <= len(object_literal) <= 180:
                continue
            if object_literal.casefold().startswith("you to "):
                continue  # a request to the agent, not the desired property
            if any(token in object_literal for token in ("{", "}", "=", "<", ">")):
                continue  # a pasted code edit is not a design preference
            if _IMPLEMENTATION_DETAIL.search(object_literal):
                continue
            visual = bool(_VISUAL.search(object_literal))
            workflow = bool(_WORKFLOW.search(object_literal))
            if not visual and not workflow:
                continue
            verb = re.sub(r"\s+", " ", match.group("verb").casefold())
            if (
                workflow
                and not verb.startswith("i ")
                and verb != "no"
                and not re.search(r"\b(autonom(?:ous|ously)|without me)\b", object_literal, re.I)
            ):
                continue  # implementation commands are not durable workflow tastes
            if visual and not workflow:
                # A component edit is a task, not an enduring design preference.
                # Retain only reusable style direction, and keep it project-scoped.
                if (
                    match.group("verb").casefold() != "simplify"
                    and not _REUSABLE_STYLE.search(object_literal)
                ):
                    continue
                if _SUBPART_COLOUR.search(object_literal):
                    continue
                if _LOCAL_ELEMENT.match(object_literal) and not _BROAD_TARGET.search(
                    object_literal
                ):
                    continue
            quote_start = match.start("verb")
            quote_end = match.end() + len(object_literal)
            quote = line[quote_start:quote_end]
            covered_until = quote_end
            result.append(
                ProjectGuidancePhrase(
                    quote=quote,
                    # The imperative is part of the meaning. Without it,
                    # "Simplify the UI" was stored as "the UI, improve it" and
                    # "No weekly evaluation" as the ambiguous "weekly evaluation".
                    object_literal=quote,
                    dimension="workflow" if workflow else "visual",
                    valence=(
                        "avoid"
                        if verb in {
                            "remove", "avoid", "no", "don't", "do not", "i don't want",
                            "i do not want",
                        }
                        else "prefer"
                    ),
                    char_start=offset + quote_start,
                    char_end=offset + quote_end,
                )
            )
        offset += len(line)
    return tuple(result)


def communication_guidance_phrases(body: str) -> tuple[ProjectGuidancePhrase, ...]:
    """Owner-authored, reusable answer-style directions; no inferred approvals.

    Keep the whole sentence when the owner contrasts what to omit with what to
    provide. A one-off request for a concise *plan* is not a standing style
    instruction, and an assistant's words never enter this lane.
    """
    separator = body.find("\n\n")
    content_start = separator + 2 if body.startswith("From:") and separator >= 0 else 0
    result: list[ProjectGuidancePhrase] = []
    in_fence = False
    offset = content_start
    for line in body[content_start:].splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            offset += len(line)
            continue
        if in_fence or stripped.startswith((">", "{", "[", "+", "@@")):
            offset += len(line)
            continue
        for match in _COMMUNICATION_DIRECTIVE.finditer(line):
            prefix = line[: match.start()]
            if prefix.count('"') % 2 or prefix.count("'") % 2:
                continue
            if prefix.strip() and not re.search(r"[.!?,]\s*$", prefix):
                continue
            remainder = line[match.start() :]
            ending = re.search(r"[.!?;\r\n]", remainder)
            quote = (remainder[: ending.start()] if ending else remainder).strip()
            if not 8 <= len(quote) <= 220 or not _COMMUNICATION_STYLE.search(quote):
                continue
            valence = "avoid" if re.match(r"(?i)(?:do\s+not|don't|dont|never)", quote) else "prefer"
            result.append(
                ProjectGuidancePhrase(
                    quote=quote,
                    object_literal=quote,
                    dimension="communication",
                    valence=valence,
                    char_start=offset + match.start(),
                    char_end=offset + match.start() + len(quote),
                )
            )
        offset += len(line)
    return tuple(result)


def communication_theme(phrase: ProjectGuidancePhrase) -> str:
    """Group equivalent short-form feedback before it clutters agent lookup."""
    folded = phrase.quote.casefold()
    if "emoji" in folded:
        return "no_emoji"
    if "less random numbers" in folded or "more conclusions" in folded:
        return "conclusions_over_numbers"
    return "brevity"
