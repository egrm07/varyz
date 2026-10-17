"""Generated context envelopes shared by local and hosted authorship checks.

Keep this module stdlib-only: hosted archive reads do not install local connector
dependencies or access a system keychain.
"""

import re

_TAGS = r"hook_prompt|external_codex_apps_open_page|app-context|app_context"
_BLOCKS = re.compile(rf"<({_TAGS})(?:\s[^>]*)?>.*?</\1>", re.DOTALL | re.IGNORECASE)
_OPEN = re.compile(rf"</?({_TAGS})(?:\s[^>]*)?>", re.IGNORECASE)
_OPENING = re.compile(rf"^\s*<({_TAGS})(?:\s|>)", re.IGNORECASE)
_REVIEW_TRANSCRIPT = (
    "The following is the Codex agent history added since your last approval assessment."
)


def clean_generated_context(text: str) -> str:
    text = text.strip()
    if text.startswith(_REVIEW_TRANSCRIPT):
        return ""
    opening = _OPENING.match(text)
    if opening and not re.search(rf"</{re.escape(opening.group(1))}>", text, re.IGNORECASE):
        return ""
    return _OPEN.sub(" ", _BLOCKS.sub(" ", text)).strip()


def is_machine_entry(source: str, body: str | None) -> bool:
    if source != "ai_conversations" or not body:
        return False
    header, separator, text = body.partition("\n\n")
    if not (separator and header.startswith("From: ") and "\nConversation: " in header):
        text = body
    return not clean_generated_context(text)
