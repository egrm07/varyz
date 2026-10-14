"""Claim patterns in the languages the archive is actually written in.

The deterministic extractor used to read English only. On a real archive that
is not a limitation, it is a distortion: 17,905 of this machine's authored
WhatsApp and Telegram messages are Russian, so the graph described the part of
a life conducted in English and silently dropped the larger part conducted in
Russian. The Mirror then reported values spanning 152 days while the *people*
section — which needs no language at all — spanned 1,524. Nothing was wrong
with either number. The person was simply being read in one language.

So the patterns live here, one set per language, grouped by the predicate they
produce rather than by the language they belong to. That grouping is the
important part: precedence is *predicate-major*, so "I moved to Berlin",
«я переехал в Берлин» and "me mudé a Berlín" all resolve to `moved_to` before
any language gets a chance to claim the sentence as something else. A language
added later inherits the same precedence for free.

Three rules kept these sets honest:

**Parity over coverage.** A language gets the predicates English already has,
with the same strength of evidence — not every construction it could express.
Russian could plausibly yield `planned_to` from «хочу» ("I want to"), but
English does not read "I want to" as a plan, and a Mirror whose goals are
looser in one language than another is worse than one that reads fewer.

**No bare inflection.** Russian and Spanish both drop the subject pronoun.
Where the verb ending is unambiguously first person («живу», "vivo") the
pronoun is optional; where it is not («переехал» is equally "I moved" and "he
moved") the pronoun is required. Guessing costs the one thing the graph
exists to protect — knowing whose statement this is.

**Only what a person can check.** Every pattern captures the words that follow
it verbatim, because the Mirror shows the user their own sentence. Nothing
here translates, normalises, or paraphrases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from varyz.graph.models import ClaimModality, ObjectType

# Languages read by the deterministic extractor. Used for reporting; adding a
# code here does nothing on its own.
LANGUAGES = ("en", "ru", "es")


@dataclass(frozen=True)
class ClaimPattern:
    """One way one language says one thing."""

    predicate: str
    object_type: ObjectType
    language: str
    pattern: re.Pattern[str]


def _p(
    predicate: str, object_type: ObjectType, language: str, source: str
) -> ClaimPattern:
    return ClaimPattern(
        predicate=predicate,
        object_type=object_type,
        language=language,
        pattern=re.compile(source, re.IGNORECASE),
    )


# An object that runs to the end of the clause, and one that stops at the first
# comma. Places and projects are single phrases; plans, beliefs and requests are
# sentences and lose their meaning when cut at a comma.
_PHRASE = r"(?P<object>[^,.;!?]+)"
_CLAUSE = r"(?P<object>[^.;!?]+)"

# Russian marks a reported clause with «что» and Spanish with "que"; both are
# obligatory, so leaving them in would prefix every belief in those languages
# with a word carrying none of its meaning. The leading `\b` is load-bearing:
# without it the verb stem matches inside a longer word ("prometido" reading as
# "prometi" + "do") and invents a claim out of a suffix.
_RU_THAT = r"\b\s*,?\s*(?:что\s+)?"
_ES_THAT = r"\b\s*,?\s*(?:que\s+)?"

NAME_SAID_MOVE_RE = re.compile(
    r"\b(?P<name>[A-Z][A-Za-z'-]{1,40})\s+(?:said|reported)\s+(?:that\s+)?"
    r"(?:they|he|she)\s+(?:had\s+)?moved\s+to\s+(?P<object>[^,.;!?]+)",
    re.IGNORECASE,
)

# Predicate-major: every language's way of saying `moved_to` is tried before
# any language's way of saying `lived_in`.
CLAIM_PATTERNS: tuple[ClaimPattern, ...] = (
    _p(
        "moved_to",
        ObjectType.ENTITY,
        "en",
        r"\b(?:i|we|he|she|they)\s+"
        r"(?:(?:may|might|could|will)\s+move|(?:am|are|was|were)\s+moving|"
        rf"(?:have|had)\s+moved|moved)\s+to\s+{_PHRASE}",
    ),
    _p(
        "moved_to",
        ObjectType.ENTITY,
        "ru",
        r"\b(?:я|мы)\s+(?:уже\s+)?"
        r"(?:переехал(?:а|и)?|перееду|переедем|переезжаю|переезжаем)\s+"
        rf"(?:в|на)\s+{_PHRASE}",
    ),
    _p(
        "moved_to",
        ObjectType.ENTITY,
        "es",
        r"\b(?:me\s+he\s+mudado|nos\s+hemos\s+mudado|nos\s+mudamos|"
        rf"me\s+mudar(?:é|e)|me\s+mud(?:é|e)|me\s+mudo)\s+a\s+{_PHRASE}",
    ),
    _p(
        "lived_in",
        ObjectType.ENTITY,
        "en",
        r"\b(?:i|we|he|she|they)\s+(?:(?:am|are|was|were)\s+)?"
        rf"(?:living|lived|live)\s+in\s+{_PHRASE}",
    ),
    _p(
        "lived_in",
        ObjectType.ENTITY,
        "ru",
        rf"\b(?:я|мы)\s+(?:жил(?:а|и)?|живу|живём|живем)\s+(?:в|на)\s+{_PHRASE}",
    ),
    _p(
        "lived_in",
        ObjectType.ENTITY,
        "es",
        r"\b(?:yo\s+|nosotros\s+)?"
        r"(?:viv(?:í|i)amos|viv(?:í|i)a|vivimos|vivo|viv(?:í|i))\s+"
        rf"en\s+{_PHRASE}",
    ),
    _p(
        "worked_on",
        ObjectType.ENTITY,
        "en",
        r"\b(?:i|we)\s+(?:(?:am|are|was|were)\s+)?"
        rf"(?:working|worked|work)\s+on\s+{_PHRASE}",
    ),
    _p(
        "worked_on",
        ObjectType.ENTITY,
        "ru",
        r"\b(?:я|мы)\s+(?:сейчас\s+)?"
        rf"(?:работаю|работаем|работал(?:а|и)?)\s+над\s+{_PHRASE}",
    ),
    _p(
        "worked_on",
        ObjectType.ENTITY,
        "es",
        r"\b(?:yo\s+|nosotros\s+)?"
        r"(?:estoy\s+trabajando|estamos\s+trabajando|trabajamos|trabajo|"
        rf"trabaj(?:é|e)|trabajaba)\s+en\s+{_PHRASE}",
    ),
    _p(
        "decided_to",
        ObjectType.TEXT,
        "en",
        # "decided not to" is still a decision, and its polarity is already
        # carried separately. Russian and Spanish negate inside the object, so
        # without this English would be the one language that loses the claim.
        r"\b(?:i|we)\s+(?:have\s+|had\s+)?(?:decided|decide)\s+(?:not\s+)?to\s+"
        rf"{_CLAUSE}",
    ),
    _p(
        "decided_to",
        ObjectType.TEXT,
        "ru",
        rf"\b(?:я|мы)\s+(?:уже\s+)?реш(?:ил(?:а|и)?|или){_RU_THAT}{_CLAUSE}",
    ),
    _p(
        "decided_to",
        ObjectType.TEXT,
        "es",
        r"\b(?:he\s+decidido|hemos\s+decidido|decidimos|decid(?:í|i))"
        rf"{_ES_THAT}{_CLAUSE}",
    ),
    _p(
        "planned_to",
        ObjectType.TEXT,
        "en",
        r"\b(?:i|we)\s+(?:plan(?:ned)?|intend(?:ed)?|hope(?:d)?|"
        rf"(?:am|are|was|were)\s+going)\s+to\s+{_CLAUSE}",
    ),
    _p(
        "planned_to",
        ObjectType.TEXT,
        "ru",
        r"\b(?:я|мы)\s+(?:планиру(?:ю|ем)|планировал(?:а|и)?|собира(?:юсь|емся)|"
        r"собирал(?:ся|ась|ись)|наде(?:юсь|емся)|намерен(?:а|ы)?)\s+"
        rf"{_CLAUSE}",
    ),
    _p(
        "planned_to",
        ObjectType.TEXT,
        "es",
        r"\b(?:voy\s+a|vamos\s+a|planeo|planeamos|espero|esperamos|"
        rf"tengo\s+la\s+intenci(?:ó|o)n\s+de|pienso(?!\s+que\b))\s+{_CLAUSE}",
    ),
    _p(
        "promised_to",
        ObjectType.TEXT,
        "en",
        rf"\b(?:i|we)\s+(?:promise(?:d)?\s+to|will)\s+{_CLAUSE}",
    ),
    # Russian's analytic future («буду делать») is enumerable; its perfective
    # future («сделаю») is a whole verb class and is deliberately left out
    # rather than approximated.
    _p(
        "promised_to",
        ObjectType.TEXT,
        "ru",
        r"\b(?:я|мы)\s+(?:обеща(?:ю|ем)|обещал(?:а|и)?|бу(?:ду|дем))\s+"
        rf"{_CLAUSE}",
    ),
    _p(
        "promised_to",
        ObjectType.TEXT,
        "es",
        r"\b(?:te\s+|le\s+|les\s+|os\s+)?promet(?:emos|imos|o|(?:í|i))"
        rf"{_ES_THAT}{_CLAUSE}",
    ),
    _p(
        "requested",
        ObjectType.TEXT,
        "en",
        rf"\b(?:please|can you|could you|would you)\s+{_CLAUSE}",
    ),
    _p(
        "requested",
        ObjectType.TEXT,
        "ru",
        r"\b(?:пожалуйста|можешь|можете|сможешь|сможете|"
        rf"не\s+мог(?:ла)?\s+бы\s+ты)\s*,?\s*{_CLAUSE}",
    ),
    _p(
        "requested",
        ObjectType.TEXT,
        "es",
        r"\b(?:por\s+favor|me\s+puedes|puedes|podr(?:í|i)as|puede\s+usted)"
        rf"\s*,?\s*{_CLAUSE}",
    ),
    _p(
        "felt_about",
        ObjectType.TEXT,
        "en",
        rf"\b(?:i|we)\s+(?:feel|felt|am feeling|were feeling)\s+{_CLAUSE}",
    ),
    _p(
        "felt_about",
        ObjectType.TEXT,
        "ru",
        r"\b(?:я|мы)\s+(?:чувству(?:ю|ем)|чувствовал(?:а|и)?|ощуща(?:ю|ем))"
        rf"{_RU_THAT}{_CLAUSE}",
    ),
    _p(
        "felt_about",
        ObjectType.TEXT,
        "es",
        # "lo siento" is an apology, not a feeling about anything.
        r"\b(?:me\s+siento|nos\s+sentimos|me\s+sent(?:í|i)a|me\s+sent(?:í|i)|"
        rf"(?<!lo )siento){_ES_THAT}{_CLAUSE}",
    ),
    _p(
        "believed",
        ObjectType.TEXT,
        "en",
        # A denied belief is still a belief being reported on, and its polarity
        # is carried separately — the same reasoning `decided not to` already
        # gets below. Without this, "I don't believe software should stay local"
        # matched no pattern at all, so the only version of a conviction the
        # archive could hold was the one the person had argued against, and a
        # value they had publicly dropped went on being shown as held.
        rf"\b(?:i|we)\s+(?:do\s+not\s+|don['’]t\s+|did\s+not\s+|didn['’]t\s+"
        rf"|no\s+longer\s+|never\s+)?(?:think|thought|believe|believed)\s+{_CLAUSE}",
    ),
    _p(
        "believed",
        ObjectType.TEXT,
        "ru",
        r"\b(?:(?:я|мы)\s+(?:дума(?:ю|ем)|думал(?:а|и)?|счита(?:ю|ем)|"
        r"считал(?:а|и)?|вер(?:ю|им)|верил(?:а|и)?|уверен(?:а|ы)?)|"
        rf"мне\s+кажется){_RU_THAT}{_CLAUSE}",
    ),
    _p(
        "believed",
        ObjectType.TEXT,
        "es",
        r"\b(?:creo|creemos|cre(?:í|i)a|pienso|pensamos|pensaba|opino|"
        rf"me\s+parece){_ES_THAT}{_CLAUSE}",
    ),
)

# Anaphora. "I think so", «я думаю, что да», "creo que sí" all affirm something
# said a moment earlier; stored without that antecedent they become a belief
# whose content is the word "so". Also catches a belief that is nothing but a
# yes or a no.
CONTEXT_DEPENDENT_BELIEF_RE = re.compile(
    r"^(?:so|eso|as(?:í|i)|так|это)\b|^(?:s(?:í|i)|no|да|нет)[\s.,!?…]*$",
    re.IGNORECASE,
)

# Negation, and only where it is unambiguous. Bare "no"/«нет» ends a sentence
# as often as it negates one, so the particle form requires a word after it.
NEGATION_RE = re.compile(
    # The contracted forms are how people actually negate in a message, and
    # leaving them out scored "I don't believe X" as a positive assertion of X.
    r"\b(?:not|never|no\s+longer|did\s+not)\b"
    r"|\b(?:do|does|did|is|are|was|were|has|have|had|can|could|would|should|will)"
    r"n['’]t\b"
    r"|\b(?:won|ain|shan)['’]t\b"
    r"|\bне\s+\w|\bнет\b|\bникогда\b|\bни\s+разу\b"
    r"|\bno\s+\w|\bnunca\b|\bjam(?:á|a)s\b|\btampoco\b",
    re.IGNORECASE,
)

# Order matters and is the English order: a sentence that is both a dream and a
# plan is a dream. Each language contributes to the same rung rather than
# getting a ladder of its own.
MODALITY_RULES: tuple[tuple[re.Pattern[str], ClaimModality], ...] = (
    (
        re.compile(
            r"\b(?:dream|dreamed|dreamt|nightmare"
            r"|сон|снилось|снился|приснилось|кошмар|мечта(?:ю|л|ла)?"
            r"|sue(?:ñ|n)o|so(?:ñ|n)(?:é|e|aba|ar)|pesadilla)\b",
            re.IGNORECASE,
        ),
        ClaimModality.DREAM,
    ),
    (
        re.compile(
            r"\b(?:probably|seems|suggests|likely"
            r"|наверное|кажется|похоже|вероятно|скорее\s+всего"
            r"|probablemente|parece|seguramente|supongo)\b",
            re.IGNORECASE,
        ),
        ClaimModality.INFERENCE,
    ),
    (
        re.compile(
            r"\b(?:may|might|could|plan|intend|going to|hope"
            r"|планиру(?:ю|ем)|собира(?:юсь|емся)|намерен|наде(?:юсь|емся)"
            r"|может\s+быть|возможно"
            r"|voy\s+a|vamos\s+a|planeo|espero|quiz(?:á|a)s?|tal\s+vez"
            r"|podr(?:í|i)a)\b",
            re.IGNORECASE,
        ),
        ClaimModality.PLAN,
    ),
    (
        re.compile(
            r"\b(?:think|thought|believe|believed"
            r"|дума(?:ю|ем|л|ла|ли)|счита(?:ю|ем)|вер(?:ю|им)|уверен"
            r"|creo|cre(?:í|i)a|pienso|opino)\b",
            re.IGNORECASE,
        ),
        ClaimModality.BELIEF,
    ),
    (
        re.compile(
            r"\b(?:feel|felt|feeling"
            r"|чувству(?:ю|ем)|чувствовал|ощуща(?:ю|ем)"
            r"|siento|sent(?:í|i)|sentimos)\b",
            re.IGNORECASE,
        ),
        ClaimModality.FEELING,
    ),
)

# Trailing time expressions that belong to the sentence, not to the thing being
# named. "I moved to Berlin last year" is a move to Berlin.
TRAILING_TIME_RE = re.compile(
    r"\b(?:next month|last month|next year|last year"
    r"|(?:on|in|during|from|until|through)\s+"
    r"(?:the\s+)?\d{4}s?(?:-\d{2}(?:-\d{2})?)?"
    r"(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?"
    r"|(?:on|in|during|from|until|since|through)\s+(?:the\s+)?"
    r"(?:january|february|march|april|may|june|july|august|september|october"
    r"|november|december)\b"
    r"|(?:on|last|next|this)\s+(?:monday|tuesday|wednesday|thursday|friday"
    r"|saturday|sunday)\b"
    r"|(?:yesterday|today|tomorrow|last week|next week|this week)\b"
    r"|в\s+(?:следующем|прошлом|этом)\s+(?:месяце|году)"
    r"|в\s+\d{4}\s+году"
    r"|el\s+(?:pr(?:ó|o)ximo|pasado)\s+(?:mes|a(?:ñ|n)o)"
    r"|el\s+(?:mes|a(?:ñ|n)o)\s+(?:pr(?:ó|o)ximo|pasado)"
    r"|en\s+\d{4})\b",
    re.IGNORECASE,
)


# Where the object of a claim ends and the rest of the sentence begins.
#
# The object patterns run to the end of the sentence, so "I moved to Lisbon in
# March 2025 and I have been renting in Alfama since" filed a *place* called
# "Lisbon in March 2025 and I have been renting in Alfama since": an entity no
# second sentence will ever match, carrying a date the temporal parser had
# already read correctly off the same sentence.
#
# Only a boundary with a new subject behind it is cut. A place name may hold a
# bare "and" -- Trinidad and Tobago, Newcastle upon Tyne -- but not "and I",
# and every conjunction below starts a clause about something else.
CLAUSE_TAIL_RE = re.compile(
    r",\s*(?:and|but|so|because|which|while|although|though)\b"
    r"|\band\s+(?:i|we|he|she|they|it)\b"
    r"|\bbut\s+(?:i|we|he|she|they|it)\b"
    r"|\bso\s+(?:i|we|he|she|they)\b"
    r"|\bwhile\s+(?:i|we|he|she|they)\b"
    r"|\bbecause\b"
    # Russian.
    r"|\bи\s+(?:я|мы|он|она|они)\b"
    r"|\bно\s+(?:я|мы|он|она|они)\b"
    r"|потому что"
    # Spanish.
    r"|\by\s+(?:yo|nosotros|él|ella|ellos)\b"
    r"|\bpero\s+(?:yo|nosotros|él|ella|ellos)\b"
    r"|porque",
    re.IGNORECASE,
)


# The keyless path. When no approved model can read an entry, the semantic
# extractor keeps only those deterministic claims whose sentence states the
# predicate outright. English-only cues here would have quietly undone
# everything above: the patterns would find the Russian claim and the fallback
# would drop it, in exactly the offline mode Varyz is built to run in.
FALLBACK_CUES: dict[str, re.Pattern[str]] = {
    "decided_to": re.compile(
        r"\b(?:decided"
        r"|реш(?:ил|ила|или)"
        r"|decid(?:í|i)|decidimos|decidido)\b",
        re.IGNORECASE,
    ),
    "promised_to": re.compile(
        r"\b(?:promise(?:d)?"
        r"|обеща(?:ю|ем|л|ла|ли)|клянусь"
        r"|promet(?:o|emos|imos|ido|(?:í|i)))\b",
        re.IGNORECASE,
    ),
    "planned_to": re.compile(
        r"\b(?:plan(?:ned)?|intend(?:ed)?"
        r"|планиру(?:ю|ем)|планировал(?:а|и)?|собира(?:юсь|емся)"
        r"|собирал(?:ся|ась|ись)|намерен(?:а|ы)?"
        r"|planeo|planeamos|tengo\s+la\s+intenci(?:ó|o)n|voy\s+a|vamos\s+a)\b",
        re.IGNORECASE,
    ),
    "believed": re.compile(
        r"\b(?:believ(?:e|ed)|should|must|ought|always|never|important"
        r"|matters?|prefer|value"
        r"|вер(?:ю|им|ил|ила)|счита(?:ю|ем|л|ла)|важно|главное|ценю"
        r"|предпочитаю|должен|должна|всегда|никогда"
        r"|creo|cre(?:í|i)a|opino|importante|importa|prefiero|valoro"
        r"|deber(?:í|i)a|siempre|nunca)\b",
        re.IGNORECASE,
    ),
}

# "I feel that X" names an opinion, not an emotion — and so do «чувствую, что»
# and "siento que". The fallback keeps feelings except these.
FEELING_HEDGE_RE = re.compile(
    r"\bfeel(?:ing)?\s+(?:that|like)\b"
    r"|\bчувству(?:ю|ем),?\s+что\b|\bощуща(?:ю|ем),?\s+что\b"
    r"|\bsiento\s+que\b|\bme\s+siento\s+como\s+si\b",
    re.IGNORECASE,
)


def modality_of(sentence: str) -> ClaimModality:
    """How the sentence holds its content, in whichever language it holds it."""
    for pattern, modality in MODALITY_RULES:
        if pattern.search(sentence):
            return modality
    return ClaimModality.FACT


def polarity_of(sentence: str) -> str:
    return "negative" if NEGATION_RE.search(sentence) else "positive"


def match_claim(sentence: str) -> tuple[ClaimPattern, re.Match[str]] | None:
    """The first predicate this sentence states, tried predicate by predicate."""
    for candidate in CLAIM_PATTERNS:
        found = candidate.pattern.search(sentence)
        if found:
            return candidate, found
    return None
