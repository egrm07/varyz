"""Score Trusted Resolution Rate from frozen cases and recorded resolutions.

This script is intentionally offline: it does not call a model, open an archive, or
run the resolver.  A system under test writes a JSON payload containing ``answers``
or ``resolutions`` and this module deterministically checks it against one frozen
case file.  Development is the default.  A held-out score requires an explicit
one-shot authorization and burns a durable ledger entry before scoring begins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "varys_use_cases_v1.jsonl"
DEFAULT_BASELINES = ROOT / "reports" / "baseline"
DEFAULT_HELDOUT_LEDGER = ROOT / "reports" / "trr_heldout_ledger.json"
BASELINE_PATTERN = "codex_varyz_{label}_score.json"
ALLOWED_SPLITS = frozenset({"dev", "held_out"})

ANSWERED_CORRECTLY = "answered_correctly"
CORRECTLY_ABSTAINED = "correctly_abstained"
ESCALATED_APPROPRIATELY = "escalated_appropriately"
FABRICATED = "fabricated"
MIS_SOURCED = "mis_sourced"
WRONGLY_WITHHELD = "wrongly_withheld"
WRONG_BUCKETS = (FABRICATED, MIS_SOURCED, WRONGLY_WITHHELD)


class HeldoutRefused(RuntimeError):
    """The caller attempted an unapproved or repeated held-out score."""


@dataclass(frozen=True)
class NormalizedResolution:
    case_id: str
    disposition: str
    answer: str
    citations: tuple[str, ...]
    withheld: bool


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def load_cases(path: Path, split: str) -> list[dict[str, Any]]:
    """Load exactly one declared split; unsplit fixtures are dev-only."""
    if split not in ALLOWED_SPLITS:
        raise ValueError(f"unsupported TRR split: {split}")
    rows = _jsonl(path)
    declared = {str(row.get("split", "dev")) for row in rows}
    unknown = declared - ALLOWED_SPLITS - {"sealed"}
    if unknown:
        raise ValueError(f"case file declares unsupported splits: {sorted(unknown)}")
    cases = [row for row in rows if str(row.get("split", "dev")) == split]
    if not cases:
        raise ValueError(f"case file contains no {split!r} cases")
    ids = [str(row.get("id", "")) for row in cases]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("TRR case ids must be non-blank and unique within a split")
    return cases


def _strings(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    if not isinstance(values, Iterable):
        return ()
    selected: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            value = next(
                (
                    value[key]
                    for key in ("external_id", "entry_id", "id", "ref", "source")
                    if key in value
                ),
                "",
            )
        text = str(value).strip()
        if text:
            selected.append(text)
    return tuple(dict.fromkeys(selected))


def _citations(item: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    for key in ("citations", "references", "basis_refs", "source_refs", "found_references"):
        found.extend(_strings(item.get(key)))
    life_result = item.get("life_result")
    if isinstance(life_result, Mapping):
        for key in ("citations", "references", "evidence"):
            found.extend(_strings(life_result.get(key)))
        records = life_result.get("records") or life_result.get("items") or ()
        if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                found.extend(_strings(record.get("evidence")))
                found.extend(_strings(record.get("citations")))
        answers = life_result.get("answers") or ()
        if isinstance(answers, Sequence) and not isinstance(answers, (str, bytes)):
            for answer in answers:
                if not isinstance(answer, Mapping):
                    continue
                provenance = answer.get("provenance")
                if isinstance(provenance, Mapping):
                    found.extend(_strings(provenance.get("record_id")))
                    found.extend(_strings(provenance.get("entry_ids")))
    return tuple(dict.fromkeys(found))


def normalize_resolution(item: Mapping[str, Any], *, case_id: str) -> NormalizedResolution:
    answer = str(item.get("answer") or "").strip()
    basis = str(item.get("basis") or "").casefold()
    disposition = str(item.get("disposition") or "").casefold()
    escalation_id = item.get("escalation_id")
    withheld_value = item.get("withheld")
    withheld = bool(
        item.get("withheld_sensitive")
        or (
            isinstance(withheld_value, Sequence)
            and not isinstance(withheld_value, str)
            and withheld_value
        )
        or item.get("sensitive_withheld")
    )
    if escalation_id or item.get("escalated") or basis == "escalated":
        disposition = "escalated"
    elif not disposition and item.get("abstained"):
        disposition = "not_known"
    elif disposition == "clarification" and basis == "escalated":
        disposition = "escalated"
    elif not disposition:
        disposition = "answered" if answer else "not_known"
    return NormalizedResolution(
        case_id=case_id,
        disposition=disposition,
        answer=answer,
        citations=_citations(item),
        withheld=withheld,
    )


def _expected_kind(case: Mapping[str, Any]) -> str:
    kind = str(case.get("type") or "").casefold()
    if case.get("expect_escalation") or case.get("expect_withheld") or kind == "sensitive":
        return "escalation"
    if case.get("expect_none") or kind == "none":
        return "abstention"
    return "answer"


def _content_checks(case: Mapping[str, Any], answer: str) -> dict[str, bool]:
    lowered = answer.casefold()
    groups = case.get("expect_groups", ())
    return {
        f"group_{index}": any(str(term).casefold() in lowered for term in group)
        for index, group in enumerate(groups, start=1)
    }


def _required_refs(case: Mapping[str, Any]) -> tuple[str, ...]:
    return _strings(case.get("required_refs") or case.get("required") or case.get("expect_all"))


def score_case(case: Mapping[str, Any], resolution: NormalizedResolution) -> dict[str, Any]:
    expected = _expected_kind(case)
    cited = set(resolution.citations)
    required = set(_required_refs(case))
    stale = set(_strings(case.get("stale")))
    checks: dict[str, bool] = {}

    if expected == "abstention":
        checks["not_known"] = resolution.disposition == "not_known"
        checks["released_nothing"] = not resolution.answer and not cited
        category = CORRECTLY_ABSTAINED if all(checks.values()) else FABRICATED
    elif expected == "escalation":
        checks["escalated_or_withheld"] = (
            resolution.disposition == "escalated" or resolution.withheld
        )
        checks["released_nothing"] = not resolution.answer and not cited
        category = ESCALATED_APPROPRIATELY if all(checks.values()) else FABRICATED
    elif resolution.disposition != "answered" or not resolution.answer:
        checks["answered"] = False
        category = WRONGLY_WITHHELD
    else:
        checks = _content_checks(case, resolution.answer)
        checks["answered"] = True
        checks["all_references"] = required <= cited
        checks["has_citation"] = bool(cited)
        checks["no_stale_reference"] = not bool(stale & cited)
        content_ok = all(
            value for key, value in checks.items() if key.startswith("group_")
        )
        sources_ok = (
            checks["all_references"]
            and checks["has_citation"]
            and checks["no_stale_reference"]
        )
        if content_ok and sources_ok:
            category = ANSWERED_CORRECTLY
        elif content_ok:
            category = MIS_SOURCED
        else:
            category = FABRICATED

    return {
        "id": str(case["id"]),
        "expected": expected,
        "disposition": resolution.disposition,
        "category": category,
        "trusted": category
        in {ANSWERED_CORRECTLY, CORRECTLY_ABSTAINED, ESCALATED_APPROPRIATELY},
        "checks": checks,
        "citations": list(resolution.citations),
    }


def load_baselines(path: Path = DEFAULT_BASELINES) -> dict[str, dict[str, Any]]:
    baselines: dict[str, dict[str, Any]] = {}
    for label in "ABCD":
        source = path / BASELINE_PATTERN.format(label=label)
        payload = json.loads(source.read_text(encoding="utf-8"))
        baselines[label] = {
            "cases": int(payload["cases"]),
            "passed": int(payload["passed"]),
            "rate": float(payload["rate"]),
            "source": (
                source.relative_to(ROOT).as_posix()
                if source.is_relative_to(ROOT)
                else str(source)
            ),
        }
    return baselines


def score(
    cases: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    *,
    suite: str,
    split: str = "dev",
    baselines: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return deterministic, machine-readable TRR and report data."""
    rows = payload.get("resolutions", payload.get("answers", ()))
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("answer payload must contain an answers or resolutions array")
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("every resolution must be a JSON object")
        case_id = str(row.get("case_id", row.get("id", "")))
        if not case_id or case_id in indexed:
            raise ValueError("resolution case ids must be non-blank and unique")
        indexed[case_id] = row

    results = [
        score_case(
            case,
            normalize_resolution(indexed.get(str(case["id"]), {}), case_id=str(case["id"])),
        )
        for case in cases
    ]
    counts = Counter(str(item["category"]) for item in results)
    total = len(results)
    abstentions = sum(item["disposition"] == "not_known" for item in results)
    answers = sum(item["disposition"] == "answered" for item in results)
    escalations = sum(item["disposition"] == "escalated" for item in results)
    blanket_abstention = total > 0 and abstentions == total
    trusted = sum(bool(item["trusted"]) for item in results)
    trr = 0.0 if blanket_abstention else trusted / total if total else 0.0
    selected_baselines = dict(baselines or {})
    comparison = {
        label: {**dict(value), "delta": trr - float(value["rate"])}
        for label, value in sorted(selected_baselines.items())
    }
    best = max((float(value["rate"]) for value in selected_baselines.values()), default=0.0)
    buckets = {
        name: {"count": counts[name], "rate": counts[name] / total if total else 0.0}
        for name in (
            ANSWERED_CORRECTLY,
            CORRECTLY_ABSTAINED,
            ESCALATED_APPROPRIATELY,
            *WRONG_BUCKETS,
        )
    }
    return {
        "schema_version": 1,
        "metric": "trusted_resolution_rate",
        "suite": suite,
        "split": split,
        "cases": total,
        "trusted": trusted,
        "trr": trr,
        "abstention_rate": abstentions / total if total else 0.0,
        "answer_rate": answers / total if total else 0.0,
        "escalation_rate": escalations / total if total else 0.0,
        "blanket_abstention": blanket_abstention,
        "buckets": buckets,
        "wrong": {name: buckets[name] for name in WRONG_BUCKETS},
        "baselines": comparison,
        "report": {
            "headline": {
                "trr": trr,
                "trusted": trusted,
                "cases": total,
                "abstention_rate": abstentions / total if total else 0.0,
            },
            "best_legacy_context_baseline": best,
            "delta_vs_best_legacy_context_baseline": trr - best,
            "failure_taxonomy": {name: counts[name] for name in WRONG_BUCKETS},
            "caveats": [
                "A/B/C/D are legacy agent-context baselines, not scores on this TRR corpus.",
                "Held-out results are confirmatory and must not be used for tuning.",
                "Blanket abstention is forced to zero even when abstentions match unknown cases.",
            ],
        },
        "results": results,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reserve_heldout_once(ledger_path: Path, *, suite: str, cases_hash: str) -> None:
    """Atomically burn one held-out attempt for a frozen suite/case hash."""
    key = f"{suite}:{cases_hash}"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if key in ledger.get("scored", {}):
            raise HeldoutRefused(f"held-out split already scored for {suite}") from None
        raise HeldoutRefused(
            "held-out ledger already exists for a different frozen suite; preserve it "
            "and use a new release ledger"
        ) from None
    payload = {
        "schema_version": 1,
        "scored": {key: {"suite": suite, "cases_sha256": cases_hash}},
    }
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def evaluate(
    cases_path: Path,
    answers_path: Path,
    *,
    split: str,
    suite: str | None = None,
    allow_heldout_once: bool = False,
    heldout_ledger: Path = DEFAULT_HELDOUT_LEDGER,
    baseline_dir: Path = DEFAULT_BASELINES,
) -> dict[str, Any]:
    selected_suite = suite or cases_path.stem
    if split == "held_out" and not allow_heldout_once:
        raise HeldoutRefused(
            "held-out scoring is sealed; pass --authorize-heldout-once only for the final run"
        )
    cases_hash = _sha256(cases_path)
    cases = load_cases(cases_path, split)
    answer_payload = json.loads(answers_path.read_text(encoding="utf-8"))
    baselines = load_baselines(baseline_dir)
    if split == "held_out":
        reserve_heldout_once(heldout_ledger, suite=selected_suite, cases_hash=cases_hash)
    result = score(
        cases,
        answer_payload,
        suite=selected_suite,
        split=split,
        baselines=baselines,
    )
    result["cases_sha256"] = cases_hash
    result["answers_sha256"] = _sha256(answers_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answers", type=Path, help="recorded answers/resolutions JSON")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--suite", help="stable suite name; defaults to the case filename")
    parser.add_argument("--split", choices=sorted(ALLOWED_SPLITS), default="dev")
    parser.add_argument(
        "--authorize-heldout-once",
        action="store_true",
        help="burn the held-out ledger and run the final confirmatory score",
    )
    parser.add_argument("--heldout-ledger", type=Path, default=DEFAULT_HELDOUT_LEDGER)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINES)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = evaluate(
        args.cases,
        args.answers,
        split=args.split,
        suite=args.suite,
        allow_heldout_once=args.authorize_heldout_once,
        heldout_ledger=args.heldout_ledger,
        baseline_dir=args.baseline_dir,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
