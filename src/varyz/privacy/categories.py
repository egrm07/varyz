"""The kinds of evidence a person must be asked about by name, once, each time.

The sensitivity label already keeps `sensitive` and `unclassified` entries off
every cloud path. This is the layer under that: an entry labelled `personal` —
ordinary, shareable, the bulk of an archive — can still be *about* a diagnosis,
a lawyer, a child, or something somebody asked to be kept quiet. Sending that to
a model is not the same act as sending a note about a deployment, and the
release contract says so: prominent one-time confirmation, naming what is going
where.

Three properties this deliberately has.

**It only ever asks.** Nothing here denies, redacts, or edits. It converts an
allow into an ask, which is the one direction a classifier this crude is allowed
to move a decision. A false positive costs the user one prompt; a false negative
costs them nothing they did not already have.

**One-time only.** A category found here cannot be covered by a standing grant —
`consent_requires_one_time` in the policy engine. Standing approval is for
routine and public evidence, and "I approved medical evidence once in March" is
not consent for June.

**It reads words, not meaning.** Every term below is one whose *presence* names
the subject rather than describing it: "custody", "prescribed", "subpoena". The
lists are short on purpose, English and Russian because the archive is those two
languages, and there is no inference beyond matching. It will miss a diagnosis
discussed without a clinical word in it. It is a floor, not a guarantee, and the
sensitivity label above it remains the real boundary.
"""

from __future__ import annotations

import re

# Human-readable names, used verbatim in the confirmation the user reads.
LABELS: dict[str, str] = {
    "medical": "health and medical",
    "sexual": "sexual or intimate",
    "legal": "legal",
    "financial": "detailed financial",
    "children": "concerning a child",
    "confidential": "told to you in confidence",
}

# Anchored on word boundaries; matched case-insensitively against the item's
# text. A term earns its place only if seeing it is already enough to know the
# subject — "hospital" is in, "sick" is not, because "sick of this build" is the
# more common sentence in this particular archive.
_TERMS: dict[str, tuple[str, ...]] = {
    "medical": (
        r"diagnos(?:is|ed|es)",
        r"prescri(?:bed|ption)",
        r"medication",
        r"antidepressant\w*",
        r"chemotherapy",
        r"biopsy",
        r"psychiatrist",
        r"therapist",
        r"therapy session",
        r"blood test",
        r"test results",
        r"hospitali[sz]ed",
        r"surgery",
        r"HIV",
        r"диагноз",
        r"назначил[аи]? (?:лекарств|препарат)\w*",
        r"психиатр\w*",
        r"терапевт\w*",
        r"операци[яю]",
        r"анализ[ыи] крови",
    ),
    "sexual": (
        r"sex life",
        r"sexual(?:ity|ly)?",
        r"intimate",
        r"intimacy",
        r"slept with",
        r"nudes?",
        r"contracepti\w+",
        r"pregnan\w+",
        r"abortion",
        r"интимн\w+",
        r"беременн\w+",
        r"аборт\w*",
    ),
    "legal": (
        r"lawyer",
        r"attorney",
        r"solicitor",
        r"lawsuit",
        r"subpoena",
        r"court hearing",
        r"prosecutor",
        r"plea deal",
        r"settlement agreement",
        r"non-disclosure agreement",
        r"\bNDA\b",
        r"адвокат\w*",
        r"юрист\w*",
        r"суд(?:ебн\w+|а|е)?",
        r"иск(?:ов\w+)?\b",
    ),
    "financial": (
        r"\bIBAN\b",
        r"sort code",
        r"routing number",
        r"account number",
        r"account balance",
        r"my salary",
        r"net worth",
        r"mortgage",
        r"tax return",
        r"credit score",
        r"зарплат\w+",
        r"ипотек\w+",
        r"налогов\w+ деклараци\w+",
        r"номер счёта",
        r"номер счета",
    ),
    "children": (
        r"custody",
        r"guardianship",
        r"p[ae]diatrician",
        r"birth certificate",
        r"my (?:son|daughter)'?s? (?:school|teacher|doctor|diagnosis)",
        r"опек[аиу]\w*",
        r"педиатр\w*",
        r"свидетельств\w+ о рождении",
    ),
    "confidential": (
        r"don'?t tell (?:anyone|anybody|him|her|them)",
        r"between (?:you and me|us)",
        r"keep (?:this|it) (?:secret|between us|to yourself)",
        r"in confidence",
        r"strictly confidential",
        r"off the record",
        r"не говори (?:никому|об этом никому)",
        r"никому не говори",
        r"по секрету",
        r"строго конфиденциально",
        r"между нами",
    ),
}

# The word boundaries the terms above are written for. Without them the terms
# match inside longer words, and one of them matched a word varyz puts in every
# prompt it builds: "archive" contains "hiv", so every cloud call declared
# health and medical evidence. A one-time confirmation that fires on everything
# is not a confirmation -- it is a key someone learns to press -- so the cost of
# the missing word boundary was the whole mechanism, not one wrong prompt.
_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile("|".join(rf"\b(?:{term})\b" for term in terms), re.IGNORECASE)
    for name, terms in _TERMS.items()
}


def detect(text: str) -> tuple[str, ...]:
    """The categories present in one piece of text, in a stable order.

    Stable because a decision's reason codes end up in a receipt and in a hash;
    an order that depended on the text would make two identical requests look
    like different ones.
    """
    if not text:
        return ()
    return tuple(name for name in LABELS if _PATTERNS[name].search(text))


def describe(categories: tuple[str, ...]) -> str:
    """The phrase shown to the user, e.g. "legal and health and medical"."""
    names = [LABELS[name] for name in categories if name in LABELS]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]
