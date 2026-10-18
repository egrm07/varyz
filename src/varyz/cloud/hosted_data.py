"""Conservative Cloud-only prohibited-data guard, not a compliance classifier.

Detected credentials/cards, explicit government identifiers, and clearly attributed
health, explicit special-category or under-16 personal records are refused.
Unlabelled, multilingual or encoded
records can escape these heuristics. Age, identity, legal consent and the absence of
sensitive data cannot be established by this scanner. Keep the service policy and
explicit upload selection; never advertise this as comprehensive DLP or age checking.
Local records and privacy exports are outside this guard.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any
from urllib.parse import unquote

from varyz.privacy.secrets_scan import MAX_SCAN_CHARS, redact_tree

POLICY = (
    "Varyz Cloud does not accept credentials, payment-card numbers, government "
    "identifiers, personal health or other special-category records, or personal data "
    "about children below "
    "the applicable digital-consent age. Do not upload these records. "
    "Automated checks are incomplete; review selected data before syncing."
)


class HostedDataRejected(ValueError):
    """Safe to display: only the detected category, never a matched private value."""


_GOVERNMENT = re.compile(
    r"\b(?:ssn|social[ _-]*security(?:[ _-]*(?:number|no))?|bsn|burgerservicenummer|"
    r"passport[ _-]*(?:number|no)|national[ _-]*id(?:entification)?(?:[ _-]*number)?|"
    r"driver'?s?[ _-]*licen[sc]e[ _-]*(?:number|no))"
    r"\s*[\"']?\s*(?::|=|\bis\b|\bnumber\b|\bno\.?\b)\s*[\"']?"
    r"(?=[a-z0-9 -]{5,25}(?:\b|$))(?=[a-z0-9 -]*\d)[a-z0-9][a-z0-9 -]{4,24}\b"
)
_IDENTITY = re.compile(
    r"\b(?:patient[ _-]*(?:name|id)|full[ _-]*name|child[ _-]*(?:name|id)|"
    r"email|date[ _-]*of[ _-]*birth)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',;:{}\[\]]+"
)
_HEALTH_FIELD = re.compile(
    r"\b(?:diagnosis|diagnoses|medical[ _-]*record|patient[ _-]*record|"
    r"prescribed[ _-]*medication|medical[ _-]*history)\s*[\"']?\s*[:=]\s*"
    r"[\"']?[^\s\"',;:{}\[\]]+"
)
_PERSONAL_HEALTH = re.compile(
    r"\b(?:i|my (?:wife|husband|partner|son|daughter)) "
    r"(?:was|am|is|has been|have been) (?:diagnosed with|treated for)\b"
)
_CHILD_PERSONAL = re.compile(
    r"\b(?:i am|i'm|my age is) (?P<self_age>\d{1,2}) (?:years? old|y/?o)\b|"
    r"\bmy (?P<relative_age>\d{1,2})[ -]year[ -]old (?:child|son|daughter)\b|"
    r"\b(?:my|our) (?:child|son|daughter) is (?P<child_age>\d{1,2}) years? old\b|"
    r"\b[a-z][a-z'-]+ is (?P<contact_age>\d{1,2}) years? old "
    r"and (?:lives at|attends|goes to|has (?:email|phone))\b"
)
_AGE_FIELD = re.compile(
    r"\b(?:child[ _-]*age|patient[ _-]*age|age)\s*[\"']?\s*[:=]\s*[\"']?(\d{1,2})\b"
)
# Generic project/OAuth approval is not a separately supported Article 9 flow.
# Match attributed records, not every mention of religion, politics or medicine.
_SPECIAL_FIELDS = (
    r"religion|religious[ _-]*beliefs?|philosophical[ _-]*beliefs?|"
    r"sexual[ _-]*orientation|sex[ _-]*life|political[ _-]*(?:opinion|affiliation|belief)s?|"
    r"trade[ _-]*union(?:[ _-]*membership)?|union[ _-]*membership|"
    r"racial[ _-]*origin|ethnic[ _-]*origin|ethnicity|"
    r"genetic[ _-]*(?:profile|data)|dna[ _-]*(?:profile|sequence)|"
    r"(?:fingerprint|face|biometric)[ _-]*(?:template|identity|identifier)"
)
_SPECIAL_DECLARATION = re.compile(
    rf"\bmy (?P<field>{_SPECIAL_FIELDS})\s*(?:is|are|:|=)\s*"
    r"(?P<value>[^\n\r\";{}\[\]]{1,120})"
)
_SPECIAL_FIELD = re.compile(
    rf"\b(?:{_SPECIAL_FIELDS})\s*[\"']?\s*[:=]\s*[\"']?"
    r"(?P<value>[^\n\r\"',;:{}\[\]]{1,120})"
)
_SPECIAL_SELF = re.compile(
    r"\bi(?: am|'m) (?:gay|lesbian|bisexual|homosexual|heterosexual|"
    r"a (?:muslim|christian|buddhist|hindu|jew|socialist|communist|trade union member)|"
    r"an atheist|a member of (?:the |a )?trade union)"
    r"(?=$|[\s]*(?:[.,;!\"}\]]|and\b|who\b))"
)
_UNDISCLOSED = re.compile(
    r"^(?:unknown|unspecified|undisclosed|private|confidential|not disclosed|"
    r"prefer not to (?:say|disclose)|none of your business|null|n/?a)\b"
)


def _disclosed(value: str) -> bool:
    return bool(value.strip()) and not _UNDISCLOSED.match(value.strip())


def _text(value: Any, depth: int = 0) -> str:
    if depth > 30:
        raise HostedDataRejected("Cloud content nesting exceeds the safety scan limit")
    if isinstance(value, dict):
        # Keep field names with their values, so split structured records are checked.
        parts = [str(k) + ': "' + _text(v, depth + 1) + '"' for k, v in value.items()]
        result = "{" + "\n".join(parts) + "}"
    elif isinstance(value, (list, tuple)):
        result = "[" + "\n".join(_text(v, depth + 1) for v in value) + "]"
    elif value is None:
        result = ""
    else:
        result = str(value)
    if len(result) > MAX_SCAN_CHARS:
        raise HostedDataRejected("Cloud content exceeds the safety scan limit")
    return result


def require_hosted_safe(value: Any) -> None:
    """Reject detected prohibited records without returning or logging their content."""
    text = _text(value)
    # Run the established credential/card guard too, including its encoding checks.
    if redact_tree(value)[1]:
        raise HostedDataRejected("Cloud content contains detected credentials or payment-card data")
    text = unicodedata.normalize("NFKC", unquote(html.unescape(text))).casefold()
    if _GOVERNMENT.search(text):
        raise HostedDataRejected("Cloud content contains a detected government identifier")
    if _PERSONAL_HEALTH.search(text):
        raise HostedDataRejected("Cloud content contains a detected personal health record")
    if _SPECIAL_SELF.search(text) or any(
        _disclosed(match.group("value")) for match in _SPECIAL_DECLARATION.finditer(text)
    ):
        raise HostedDataRejected(
            "Cloud content contains a detected special-category personal record"
        )
    for match in _CHILD_PERSONAL.finditer(text):
        age = next(
            match.group(k)
            for k in ("self_age", "relative_age", "child_age", "contact_age") if match.group(k)
        )
        if 0 <= int(age) < 16:
            raise HostedDataRejected("Cloud content contains detected personal data about a child")
    for record in _record_texts(value):
        record = unicodedata.normalize("NFKC", unquote(html.unescape(record))).casefold()
        if not _IDENTITY.search(record):
            continue
        if _HEALTH_FIELD.search(record):
            raise HostedDataRejected("Cloud content contains a detected personal health record")
        if any(_disclosed(match.group("value")) for match in _SPECIAL_FIELD.finditer(record)):
            raise HostedDataRejected(
                "Cloud content contains a detected special-category personal record"
            )
        if any(int(m.group(1)) < 16 for m in _AGE_FIELD.finditer(record)):
            raise HostedDataRejected("Cloud content contains detected personal data about a child")


def _record_texts(value: Any):
    # Only combine scalar fields from the same object, not unrelated records.
    # _text has already bounded the complete tree and its nesting above.
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        yield _text({k: v for k, v in value.items() if not isinstance(v, (dict, list, tuple))})
        for item in value.values():
            yield from _record_texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _record_texts(item)
