"""Deterministic temporal normalization and claim relationship rules."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from varyz.domain import TimePrecision

ISO_DATETIME_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?"
    r"(?:Z|[+-]\d{2}:\d{2})?)\b"
)
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})(?:-(\d{2}))?\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\b")
DECADE_RE = re.compile(r"\b((?:19|20|21)\d0)s\b")
YEAR_SHORT_RANGE_RE = re.compile(
    r"\b(?P<start>(?:19|20|21)\d{2})[-–—](?P<end>\d{2})\b"
)
MONTH_RE = re.compile(
    r"\b("
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december"
    r")\s+(19\d{2}|20\d{2}|21\d{2})\b",
    re.IGNORECASE,
)
MONTH_NAME_PATTERN = (
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)"
)
TOKEN_PATTERN = (
    r"(?:"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?"
    r"|\d{4}(?:-\d{2}(?:-\d{2})?)?"
    rf"|{MONTH_NAME_PATTERN}\s+(?:19\d{{2}}|20\d{{2}}|21\d{{2}})"
    r"|(?:19|20|21)\d0s"
    r"|next\s+(?:month|year)"
    r"|last\s+(?:month|year)"
    r")"
)
RANGE_RE = re.compile(
    rf"\b(?:from\s+)?(?P<start>{TOKEN_PATTERN})\s+"
    rf"(?:to|until|through|[-–—])\s+(?P<end>{TOKEN_PATTERN})\b",
    re.IGNORECASE,
)
UNTIL_RE = re.compile(
    rf"\b(?:until|through)\s+(?P<end>{TOKEN_PATTERN})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizedTime:
    value: datetime | None
    precision: TimePrecision
    original: str | None
    inferred: bool


@dataclass(frozen=True)
class NormalizedInterval:
    valid_from: datetime | None
    valid_to: datetime | None
    precision: TimePrecision
    original: str | None
    inferred: bool


def _aware(value: datetime) -> datetime:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def add_months(value: datetime, months: int) -> datetime:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _inclusive_end(value: datetime | None, precision: TimePrecision) -> datetime | None:
    if value is None:
        return None
    if precision == TimePrecision.YEAR:
        return value.replace(
            month=12, day=31, hour=23, minute=59, second=59, microsecond=999999
        )
    if precision == TimePrecision.DECADE:
        return value.replace(
            year=value.year + 9,
            month=12,
            day=31,
            hour=23,
            minute=59,
            second=59,
            microsecond=999999,
        )
    if precision == TimePrecision.MONTH:
        return value.replace(
            day=calendar.monthrange(value.year, value.month)[1],
            hour=23,
            minute=59,
            second=59,
            microsecond=999999,
        )
    if precision == TimePrecision.DAY:
        return value.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value


def parse_time_expression(text: str, observed_at: datetime | None) -> NormalizedTime:
    """Resolve only deterministic forms and preserve the source phrase."""
    observed = _aware(observed_at) if observed_at else None
    lower = text.lower()
    relative = (
        ("next month", 1, TimePrecision.MONTH),
        ("last month", -1, TimePrecision.MONTH),
        ("next year", 12, TimePrecision.YEAR),
        ("last year", -12, TimePrecision.YEAR),
    )
    for phrase, months, precision in relative:
        if phrase in lower:
            if observed is None:
                return NormalizedTime(None, TimePrecision.UNKNOWN, phrase, True)
            resolved = add_months(observed, months)
            if precision == TimePrecision.MONTH:
                resolved = resolved.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                resolved = resolved.replace(
                    month=1, day=1, hour=0, minute=0, second=0, microsecond=0
                )
            return NormalizedTime(resolved, precision, phrase, True)

    match = ISO_DATETIME_RE.search(text)
    if match:
        surface = match.group(1)
        try:
            value = datetime.fromisoformat(surface.replace("Z", "+00:00"))
        except ValueError:
            return NormalizedTime(None, TimePrecision.UNKNOWN, surface, False)
        return NormalizedTime(_aware(value), TimePrecision.EXACT, surface, False)

    match = ISO_DATE_RE.search(text)
    if match:
        year, month, day = match.groups()
        precision = TimePrecision.DAY if day else TimePrecision.MONTH
        try:
            value = datetime(int(year), int(month), int(day or 1), tzinfo=UTC)
        except ValueError:
            return NormalizedTime(
                None, TimePrecision.UNKNOWN, match.group(0), False
            )
        return NormalizedTime(value, precision, match.group(0), False)

    match = MONTH_RE.search(text)
    if match:
        month = list(calendar.month_name).index(match.group(1).title())
        value = datetime(int(match.group(2)), month, 1, tzinfo=UTC)
        return NormalizedTime(value, TimePrecision.MONTH, match.group(0), False)

    match = YEAR_RE.search(text)
    if match:
        value = datetime(int(match.group(1)), 1, 1, tzinfo=UTC)
        return NormalizedTime(value, TimePrecision.YEAR, match.group(0), False)
    match = DECADE_RE.search(text)
    if match:
        value = datetime(int(match.group(1)), 1, 1, tzinfo=UTC)
        return NormalizedTime(value, TimePrecision.DECADE, match.group(0), False)
    return NormalizedTime(None, TimePrecision.UNKNOWN, None, False)


def parse_time_interval(
    text: str, observed_at: datetime | None
) -> NormalizedInterval:
    """Parse an explicit closed/open interval, otherwise a single time point."""
    short_year_range = YEAR_SHORT_RANGE_RE.search(text)
    if short_year_range:
        start_year = int(short_year_range.group("start"))
        end_year = start_year // 100 * 100 + int(short_year_range.group("end"))
        if end_year < start_year:
            end_year += 100
        if end_year - start_year <= 10:
            return NormalizedInterval(
                valid_from=datetime(start_year, 1, 1, tzinfo=UTC),
                valid_to=_inclusive_end(
                    datetime(end_year, 1, 1, tzinfo=UTC),
                    TimePrecision.YEAR,
                ),
                precision=TimePrecision.YEAR,
                original=short_year_range.group(0),
                inferred=False,
            )
    match = RANGE_RE.search(text)
    if match:
        start = parse_time_expression(match.group("start"), observed_at)
        end = parse_time_expression(match.group("end"), observed_at)
        if start.value is None or end.value is None:
            return NormalizedInterval(
                valid_from=None,
                valid_to=None,
                precision=TimePrecision.UNKNOWN,
                original=match.group(0),
                inferred=False,
            )
        precision = (
            start.precision
            if start.precision == end.precision
            else TimePrecision.UNKNOWN
        )
        return NormalizedInterval(
            valid_from=start.value,
            valid_to=_inclusive_end(end.value, end.precision),
            precision=precision,
            original=match.group(0),
            inferred=False,
        )
    match = UNTIL_RE.search(text)
    if match:
        end = parse_time_expression(match.group("end"), observed_at)
        return NormalizedInterval(
            valid_from=None,
            valid_to=_inclusive_end(end.value, end.precision),
            precision=end.precision,
            original=match.group(0),
            inferred=False,
        )
    point = parse_time_expression(text, observed_at)
    return NormalizedInterval(
        valid_from=point.value,
        valid_to=None,
        precision=point.precision,
        original=point.original,
        inferred=point.inferred,
    )


def intervals_overlap(
    left_from: str | None,
    left_to: str | None,
    right_from: str | None,
    right_to: str | None,
) -> bool:
    """Unknown bounds are open; two entirely unknown intervals overlap."""
    lower_left = left_from or "0000"
    upper_left = left_to or "9999"
    lower_right = right_from or "0000"
    upper_right = right_to or "9999"
    return lower_left <= upper_right and lower_right <= upper_left


# Predicates that admit one object at a time: you live in one place, and moving
# to Lisbon in 2021 is incompatible with living in Berlin in 2021. Everything
# else a person says is cumulative — you may believe, plan, decide and feel any
# number of different things at once without disagreeing with yourself, so two
# such claims with different objects are simply two claims.
FUNCTIONAL_PREDICATES = frozenset({"lived_in", "moved_to"})

# The extractor leaves the negation inside the object text — "software should
# stay local" and "software should not stay local" are stored as two different
# objects — while recording the polarity separately. Comparing objects with the
# negation removed is what lets a claim and its denial meet as the same subject
# argued both ways, which is the only pairing that deserves to be called a
# contradiction.
_OBJECT_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|n't|didn't|doesn't|don't|isn't|wasn't"
    r"|не|нет|никогда|nunca|jam(?:á|a)s|tampoco)\b",
    re.IGNORECASE,
)


def _object_key(claim: dict) -> str:
    value = claim["object_value_json"] or ""
    if claim["object_type"] != "text":
        return value
    return " ".join(_OBJECT_NEGATION_RE.sub(" ", value).split()).casefold()


def claim_relation(left: dict, right: dict) -> str | None:
    if left["subject_entity_id"] != right["subject_entity_id"]:
        return None
    if left["predicate"] != right["predicate"]:
        return None
    if not intervals_overlap(
        left["valid_from"], left["valid_to"], right["valid_from"], right["valid_to"]
    ):
        return None
    same_object = (
        left["object_type"] == right["object_type"]
        and _object_key(left) == _object_key(right)
    )
    if not same_object:
        # Two different objects under a cumulative predicate are two different
        # statements, not a disagreement. Treating them as contradicting was
        # how a person's own words came back as evidence against themselves:
        # every extra thing they believed argued with everything else they
        # believed, and a Mirror conclusion arrived already contested.
        if left["predicate"] not in FUNCTIONAL_PREDICATES:
            return None
        if left["modality"] == right["modality"]:
            return "contradicts"
        if {left["modality"], right["modality"]} <= {"fact", "report"}:
            return "contradicts"
        return None
    if left["polarity"] != right["polarity"]:
        # The same object, asserted and denied. This is the disagreement.
        if left["modality"] == right["modality"]:
            return "contradicts"
        if {left["modality"], right["modality"]} <= {"fact", "report"}:
            return "contradicts"
        return None
    if left["modality"] == right["modality"]:
        return "duplicates"
    modalities = {left["modality"], right["modality"]}
    if modalities == {"plan", "fact"}:
        plan = left if left["modality"] == "plan" else right
        fact = left if left["modality"] == "fact" else right
        plan_time = plan["observed_at"] or plan["asserted_at"] or plan["valid_from"]
        fact_time = fact["observed_at"] or fact["asserted_at"] or fact["valid_from"]
        if plan_time and fact_time and fact_time >= plan_time:
            return "supersedes"
        return "supports"
    if modalities == {"report", "fact"}:
        return "supports"
    return None
