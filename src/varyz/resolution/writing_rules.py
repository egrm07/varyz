"""Rules about how text reads hold for every text an agent writes, not only tasks that name them.

Ranking by shared words (``rule_selection``) suits rules about a kind of work:
"dry-run first" bears on a task that says "build". It fails rules about the
text itself. "Do not use em dashes" shares no word with "is this security
email legit?", yet the answer and any reply drafted in it are text for the
owner. So two kinds of rule leave the ranking:

- text rules: how any text reads, character by character (em dashes, emoji,
  exclamation marks, title case, spelling). Every briefing shows them, and
  every later prompt in a session repeats them;
- message rules: how the owner wants an email or message to read (short,
  honest, not a pitch). They are shown whenever the work involves an email or
  a message. The owner often states them for one kind of message ("creator
  outreach emails"); the agent is told to carry their style over to the email
  in hand and skip what only fits that kind.

Both stay inside scope, as every rule does: a rule for another project never
applies. Two marks are plain enough to check in a finished reply (``breaches``):
an em dash and an emoji, when an in-scope rule forbids them.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field

from varyz.resolution.knowledge import GLOBAL_SCOPE
from varyz.resolution.rule_selection import RuleLike, collapse

MAX_TEXT_RULES = 8
MAX_MESSAGE_RULES = 8

TEXT_TITLE = "How the owner wants text written (every reply, draft and comment)"
MESSAGE_TITLE = "How the owner wants emails and messages written"
MESSAGE_NOTE = (
    "Some of these were stated for one kind of message (outreach, invitations). "
    "Write any email or message for the owner in their style: length, tone, honesty "
    "and punctuation carry over; skip only parts that fit that kind alone."
)

# Evidence the reader appends to a learned rule: "(3 messages, latest 2026-10-08)".
_EVIDENCE = re.compile(r"\s*\(\d+ messages?, latest [^)]*\)\s*$", re.IGNORECASE)
_TYPOGRAPHY = re.compile(
    r"(?<![\w-])(?:em[ -]?dash(?:es)?|en[ -]?dash(?:es)?|emojis?|emoticons?|"
    r"exclamation(?: marks?| points?)?|oxford commas?|serial commas?|title case|"
    r"sentence case|all caps|capitali[sz](?:e|ed|ation)|british spelling|"
    r"american spelling|curly quotes|smart quotes)(?![\w-])",
    re.IGNORECASE,
)
_MESSAGE_NOUN = re.compile(
    r"(?<![\w-])(?:e-?mails?|messages?|replies|letters?|dms?|newsletters?|"
    r"invitations?|cover letters?)(?![\w-])",
    re.IGNORECASE,
)
_MESSAGE_SHAPE = re.compile(
    r"(?<![\w-])(?:short|shorter|concise|brief|length|tone|warm|friendly|polite|"
    r"formal|informal|casual|honest|pitch\w*|salesy|hedg\w*|caveats?|nuances?|"
    r"uncertainty|sign[- ]?off|greeting|voice|sound\w*|simple|plain|human|natural|"
    r"authentic|personali[sz]\w*|em dash\w*|emojis?)(?![\w-])",
    re.IGNORECASE,
)
# Work that involves writing or answering an email or a message.
_MESSAGE_WORK = re.compile(
    r"(?<![\w-])(?:e-?mails?|inbox|gmail|outlook|messages?|replies|reply|respond|"
    r"letters?|dms?|newsletters?|outreach|invitations?|draft\w*|whatsapp|telegram|"
    r"slack|linkedin)(?![\w-])",
    re.IGNORECASE,
)
_FORBIDS = re.compile(
    r"(?<![\w-])(?:do not|don't|dont|never|no|avoid|without|must not|mustn't|"
    r"stop using|not use|remove|replace)(?![\w-])",
    re.IGNORECASE,
)
_EM_DASH_MARK = chr(0x2014)
_EM_DASH = re.compile(r"(?<![\w-])em[ -]?dash(?:es)?(?![\w-])", re.IGNORECASE)
_EMOJI_WORD = re.compile(r"(?<![\w-])emojis?(?![\w-])", re.IGNORECASE)
_FENCED = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_QUOTED_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF✅❌❎✨❓-❕❗⭐⭕"
    "⏩-⏳⌚⌛]|[☀-➿]️"
)


def _statement(rule: RuleLike) -> str:
    return _EVIDENCE.sub("", unicodedata.normalize("NFKC", rule.statement))


def is_text_rule(rule: RuleLike) -> bool:
    """A rule about how any text reads: a punctuation mark, emoji, case or spelling."""
    return rule.kind == "rule" and bool(_TYPOGRAPHY.search(_statement(rule)))


def is_message_rule(rule: RuleLike) -> bool:
    """A rule about how an email or message should read, for any kind of one."""
    text = _statement(rule)
    return (rule.kind == "rule" and bool(_MESSAGE_NOUN.search(text))
            and bool(_MESSAGE_SHAPE.search(text)))


def involves_messages(text: str) -> bool:
    return bool(_MESSAGE_WORK.search(text))


@dataclass
class WritingRules[R: RuleLike]:
    text: list[R] = field(default_factory=list)
    messages: list[R] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {rule.id for rule in (*self.text, *self.messages)}


def _in_scope(rule: RuleLike, project_scope: str | None, lowered_task: str) -> bool:
    if rule.scope == GLOBAL_SCOPE or rule.scope == project_scope:
        return True
    return bool(re.search(rf"(?<!\w){re.escape(rule.scope)}(?!\w)", lowered_task))


def select_writing[R: RuleLike](
    rules: Sequence[R],
    task: str,
    *,
    project: str | None = None,
    domains: Collection[str] = (),
    context: Iterable[str] = (),
    normalize_scope: Callable[[str], str] | None = None,
) -> WritingRules[R]:
    """The text rules always; the message rules when the task, or ``context``
    (the session's task, say), involves an email or a message, or the work is
    writing. A rule filed under another kind of work stays with its checklist."""
    project_scope = (normalize_scope or (lambda value: value))(project) if project else None
    lowered = unicodedata.normalize("NFKC", task).casefold()
    wanted = set(domains)
    messages_wanted = "writing" in wanted or any(
        involves_messages(text) for text in (task, *context)
    )
    text_rules: list[R] = []
    message_rules: list[R] = []
    for rule in rules:
        if rule.domain not in {"", "writing"}:
            continue
        if not _in_scope(rule, project_scope, lowered):
            continue
        if is_text_rule(rule):
            text_rules.append(rule)
        elif messages_wanted and is_message_rule(rule):
            message_rules.append(rule)

    def ordered(found: list[R], cap: int) -> list[R]:
        collapsed = {id(rule) for rule in collapse(found)[0]}
        kept = [rule for rule in found if id(rule) in collapsed]
        # Owner rules before trials, this project's own before general ones, then
        # newest first (callers pass newest first).
        kept.sort(key=lambda rule: (bool(rule.trial), rule.scope == GLOBAL_SCOPE))
        return kept[:cap]

    return WritingRules(ordered(text_rules, MAX_TEXT_RULES),
                        ordered(message_rules, MAX_MESSAGE_RULES))


def forbidden_marks(rules: Iterable[RuleLike]) -> dict[str, str]:
    """Marks an in-scope owner rule plainly forbids, as mark name -> the rule's id.
    A rule on trial is shown and followed, but never stops a finished reply."""
    marks: dict[str, str] = {}
    for rule in rules:
        text = _statement(rule)
        if rule.trial or not _FORBIDS.search(text):
            continue
        if "em dash" not in marks and _EM_DASH.search(text):
            marks["em dash"] = rule.id
        if "emoji" not in marks and _EMOJI_WORD.search(text):
            marks["emoji"] = rule.id
    return marks


def prose(text: str) -> str:
    """What the reader reads as the agent's own words: no code, no quoted lines."""
    return _QUOTED_LINE.sub("", _INLINE_CODE.sub("", _FENCED.sub("", text)))


def breaches(reply: str, marks: Collection[str]) -> list[str]:
    """The forbidden marks the reply's own prose uses."""
    words = prose(reply)
    found = []
    if "em dash" in marks and _EM_DASH_MARK in words:
        found.append("em dash")
    if "emoji" in marks and _EMOJI.search(words):
        found.append("emoji")
    return found


def render_rows(rules: Iterable[RuleLike]) -> list[str]:
    return [f"- {_statement(rule)} ({'on trial, ' if rule.trial else ''}id {rule.id})"
            for rule in rules]


def reminder[R: RuleLike](found: WritingRules[R]) -> str:
    """The compact block a later prompt in a session carries."""
    parts: list[str] = []
    if found.text:
        parts.append("## " + TEXT_TITLE + "\n" + "\n".join(render_rows(found.text)))
    if found.messages:
        parts.append("## " + MESSAGE_TITLE + "\n" + MESSAGE_NOTE + "\n"
                     + "\n".join(render_rows(found.messages)))
    if not parts:
        return ""
    return ("varyz: the owner's writing rules still apply to this reply and anything "
            "you draft in it.\n\n" + "\n\n".join(parts))


__all__ = [
    "MAX_MESSAGE_RULES", "MAX_TEXT_RULES", "MESSAGE_NOTE", "MESSAGE_TITLE", "TEXT_TITLE",
    "WritingRules", "breaches", "forbidden_marks", "involves_messages", "is_message_rule",
    "is_text_rule", "prose", "reminder", "render_rows", "select_writing",
]
