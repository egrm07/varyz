"""`varyz wrapped`: the "My Way" card, built only from the owner's own rules.

Everything here runs offline against a database built in a temp directory. The
card must never reach a project rule the owner did not name, never paraphrase a
rule, never write in a dry run, and never open a socket.
"""

from __future__ import annotations

import hashlib
import itertools
import random
import socket
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from varyz.card import (
    CardRule,
    EmptyCardError,
    HandleError,
    LedgerCounts,
    OwnerRules,
    SelectionError,
    classify,
    compose,
    fit_lines,
    load_owner_rules,
    normalize_handle,
    plan_card,
    preview_lines,
    select,
    to_svg,
    write_card,
)
from varyz.card.archetype import ARCHETYPES, PAIRS, SIGNALS
from varyz.card.compose import HEADLINE, card_text, stands_alone
from varyz.card.layout import (
    CLOSE_QUOTE,
    ELLIPSIS,
    LIST_LABEL,
    OPEN_QUOTE,
    SPECS,
    Box,
    Text,
    balanced_lines,
    heuristic_measure,
)
from varyz.card.source import open_read_only
from varyz.cli import app
from varyz.db.connection import open_db
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="stdio",
)
DEVICE = "device:test"

GENERAL = (
    "Never send personal data to a cloud API without asking first.",
    "Secrets live in the OS keychain and are never logged.",
    "Privacy fails closed: unclassified data stays on this machine.",
    "No analytics, no telemetry, no tracking pixels.",
    "Always run the offline test suite before calling anything done.",
    "Do not create git commits unless asked.",
)
PROJECT = "Acme's dashboard uses their brand teal."
REPEATED = "Dry-run first: the whole loop runs with zero API keys."


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    return path


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "varyz.db"


@pytest.fixture
def ids(tmp_path: Path) -> Iterator[dict[str, str]]:
    """A fixture ledger: general rules, one project rule, one rule said three times."""
    conn = open_db(_db_path(tmp_path))
    try:
        store = SQLitePrincipleStore(conn)
        found: dict[str, str] = {}
        for text in GENERAL:
            found[text] = store.add(
                statement=text, caller=OWNER, device_id=DEVICE, source="manual"
            )
        found[PROJECT] = store.add(
            statement=PROJECT, caller=OWNER, device_id=DEVICE, source="manual", scope="acme"
        )
        # Said, withdrawn, said again, withdrawn, said a third time.
        for _ in range(2):
            rule_id = store.add(
                statement=REPEATED, caller=OWNER, device_id=DEVICE, source="manual"
            )
            store.retract(rule_id, caller=OWNER, device_id=DEVICE, reason="test")
        found[REPEATED] = store.add(
            statement=REPEATED, caller=OWNER, device_id=DEVICE, source="manual"
        )
        found["answer"] = SQLiteKnowledgeStore(conn).remember(
            question="Which analytics should sites use?",
            answer="None.",
            caller=OWNER,
            basis=AnswerBasis.EXPLICIT,
            source="manual",
            device_id=DEVICE,
        )
        yield found
    finally:
        conn.close()


def _rule(n: int, text: str, *, scope: str = "*", domain: str = "", stated: int = 1) -> CardRule:
    return CardRule(
        id=f"01TEST{n:020d}",
        text=text,
        kind="rule",
        scope=scope,
        domain=domain,
        created_at=f"2026-09-{n + 1:02d}T00:00:00Z",
        stated=stated,
    )


def _answer(n: int, question: str, answer: str) -> CardRule:
    return CardRule(
        id=f"01TEST{n:020d}",
        text=f"{question} {answer}",
        kind="answer",
        scope="*",
        domain="",
        created_at=f"2026-09-{n + 1:02d}T00:00:00Z",
        core=answer,
    )


def _owner(*rules: CardRule) -> OwnerRules:
    return OwnerRules(tuple(rules), LedgerCounts(len(rules)))


def _run(tmp_path: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["wrapped", *args, "--config", str(_config(tmp_path))])
    return result.exit_code, result.output


# -- reading ------------------------------------------------------------------------


def test_reads_only_current_owner_rules_and_counts_a_rule_said_again(
    tmp_path: Path, ids: dict[str, str]
) -> None:
    owner = load_owner_rules(_db_path(tmp_path))
    by_text = {rule.text: rule for rule in owner.rules}
    assert set(by_text) == {*GENERAL, PROJECT, REPEATED, "Which analytics should sites use? None."}
    assert by_text[REPEATED].stated == 3
    assert by_text[REPEATED].id == ids[REPEATED]
    assert by_text[PROJECT].scope == "acme" and not by_text[PROJECT].general
    assert all(rule.stated == 1 for rule in owner.rules if rule.text != REPEATED)
    assert owner.counts.rules == len(owner.rules)


def test_a_rule_an_earlier_setup_seeded_is_not_the_owners(
    tmp_path: Path, ids: dict[str, str]
) -> None:
    conn = open_db(_db_path(tmp_path))
    try:
        store = SQLitePrincipleStore(conn)
        store.add(statement="Add a favicon.", caller=OWNER, device_id=DEVICE,
                  source="standards_seed", domain="website")
        store.add(statement="Write the test first.", caller=OWNER, device_id=DEVICE,
                  source="standards_seed")
        store.add(statement="Write the test first.", caller=OWNER, device_id=DEVICE,
                  source="manual")
    finally:
        conn.close()

    owner = load_owner_rules(_db_path(tmp_path))
    texts = [rule.text for rule in owner.rules]
    assert "Add a favicon." not in texts
    assert next(rule for rule in owner.rules if rule.text == "Write the test first.").stated == 1


def test_the_connection_cannot_write(tmp_path: Path, ids: dict[str, str]) -> None:
    conn = open_read_only(_db_path(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM owner_principles")
    finally:
        conn.close()


def test_trials_and_automatic_promotions_are_not_owner_confirmed_card_rules(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    conn = open_db(path)
    try:
        store = SQLitePrincipleStore(conn)
        candidate = store.propose(statement="Always verify the example output.",
                                  source="agent:profile", caller=OWNER, device_id=DEVICE)
        trial = store.start_trial(candidate, signals=["two conversations"],
                                  caller=OWNER, device_id=DEVICE)
        assert trial not in {r.id for r in load_owner_rules(path).rules}
        promoted = store.end_trial(trial, how="promoted", caller=OWNER, device_id=DEVICE)
        assert promoted not in {r.id for r in load_owner_rules(path).rules}
        candidate = store.propose(statement="Always check the rendered example.",
                                  source="agent:profile", caller=OWNER, device_id=DEVICE)
        trial = store.start_trial(candidate, signals=["two conversations"],
                                  caller=OWNER, device_id=DEVICE)
        kept = store.confirm(trial, caller=OWNER, device_id=DEVICE)
        owner = load_owner_rules(path)
        assert [r.id for r in owner.rules] == [kept]
        assert owner.rules[0].stated == 1
    finally:
        conn.close()


def test_a_missing_database_is_not_created(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_owner_rules(_db_path(tmp_path))
    assert not _db_path(tmp_path).exists()


# -- archetype ------------------------------------------------------------------------


def test_archetype_is_deterministic_and_explained() -> None:
    rules = [
        _rule(1, "Never send personal data to a cloud API."),
        _rule(2, "Secrets are never logged."),
        _rule(3, "No telemetry."),
        _rule(4, "Keep the README short."),
    ]
    first = classify(rules)
    for seed in range(5):
        shuffled = list(rules)
        random.Random(seed).shuffle(shuffled)
        again = classify(shuffled)
        assert again.archetype == first.archetype
        assert again.explain() == first.explain()
    assert first.archetype == ARCHETYPES["guard"]
    text = "\n".join(first.explain())
    assert "no model, no randomness" in text
    assert "privacy and permission" in text and "telemetry" in text
    assert first.evidence == "3 rules are about privacy and permission."


def test_ties_are_broken_by_the_fixed_order_and_say_so() -> None:
    rules = [
        _rule(1, "No telemetry."),
        _rule(2, "Secrets stay local-first."),
        _rule(3, "Measure against a baseline."),
        _rule(4, "Every change needs tests."),
        _rule(5, "Run a preflight doctor."),
        _rule(6, "Keep a rollback ready."),
    ]
    verdict = classify(rules)
    # Three families level at 2: the fixed order pairs privacy with proof.
    assert verdict.archetype == ARCHETYPES["auditor"]
    assert "Tied with checks before acting" in verdict.reason


def test_careful_and_finishing_running_level_is_the_paranoid_shipper() -> None:
    rules = [
        _rule(1, "No telemetry."),
        _rule(2, "Secrets are never logged."),
        _rule(3, "Personal data stays private."),
        _rule(4, "Never ship placeholders."),
        _rule(5, "Done means deployed."),
        _rule(6, "Release notes come first."),
    ]
    verdict = classify(rules)
    assert verdict.archetype == ARCHETYPES["shipper"]
    assert "run level" in verdict.reason


def test_every_pairing_has_its_own_title() -> None:
    keys = [signal.key for signal in SIGNALS]
    pairs = {frozenset(pair) for pair in itertools.combinations(keys, 2)}
    assert set(PAIRS) == pairs
    assert len(set(PAIRS.values())) == len(pairs) == 21
    # 7 single streaks + 21 pairings + the fallback, all different.
    assert len(ARCHETYPES) == 29
    assert len({item.title for item in ARCHETYPES.values()}) == 29
    assert len({item.line for item in ARCHETYPES.values()}) == 29
    for families, key in PAIRS.items():
        first, second = sorted(families)
        rules = [
            _rule(1, f"{_SAMPLE[first][0]}."),
            _rule(2, f"{_SAMPLE[first][1]}."),
            _rule(3, f"{_SAMPLE[second][0]}."),
            _rule(4, f"{_SAMPLE[second][1]}."),
        ]
        assert classify(rules).archetype == ARCHETYPES[key], families


def test_a_pairing_needs_both_families_to_run_level() -> None:
    rules = [
        _rule(1, "No telemetry."),
        _rule(2, "Secrets are never logged."),
        _rule(3, "Personal data stays private."),
        _rule(4, "Encrypt the backups."),
        _rule(5, "Every change needs tests."),
        _rule(6, "Measure against a baseline."),
    ]
    # 5 about privacy against 2 about proof is not level: the Gatekeeper, not the Auditor.
    assert classify(rules).archetype == ARCHETYPES["guard"]


# Two rules per family that match that family's words and no other family's.
_SAMPLE = {
    "guard": ("No telemetry", "Secrets stay private"),
    "proof": ("Every change needs tests", "Measure against a baseline"),
    "pilot": ("Run a preflight doctor", "Keep a rollback ready"),
    "finish": ("Never ship placeholders", "Done means deployed"),
    "autopilot": ("It runs unattended", "A watchdog restarts it overnight"),
    "lean": ("Keep it simple", "Prefer the stdlib"),
    "craft": ("Pick the fonts with care", "Check the layout on mobile"),
}


def test_checks_and_finishing_running_level_is_the_release_captain() -> None:
    rules = [
        _rule(1, "Run a dry-run before anything goes live."),
        _rule(2, "Ask before publishing."),
        _rule(3, "Confirm with me before spending."),
        _rule(4, "Never ship placeholders."),
        _rule(5, "Done means deployed."),
        _rule(6, "Release notes come first."),
    ]
    assert classify(rules).archetype == ARCHETYPES["captain"]


def test_few_signals_make_the_pragmatist() -> None:
    verdict = classify(
        [_rule(1, "Use British spelling."), _rule(2, "Meeting notes go in the wiki.")]
    )
    assert verdict.archetype == ARCHETYPES["pragmatist"]
    assert "no single streak" in verdict.reason


def test_a_domain_counts_towards_its_family() -> None:
    verdict = classify([_rule(1, "Teal.", domain="website"), _rule(2, "Serif.", domain="docs")])
    assert verdict.archetype == ARCHETYPES["craft"]


# -- selection ------------------------------------------------------------------------


def test_general_rules_only_by_default(tmp_path: Path, ids: dict[str, str]) -> None:
    owner = load_owner_rules(_db_path(tmp_path))
    plan = plan_card(owner)
    assert ids[PROJECT] not in {rule.id for rule in plan.selection.chosen}
    assert ids[PROJECT] in {rule.id for rule in plan.selection.project_left_off}
    for layout in plan.layouts:
        assert PROJECT not in " ".join(layout.texts())
        assert "Acme" not in to_svg(layout)


def test_a_project_rule_joins_only_when_named(tmp_path: Path, ids: dict[str, str]) -> None:
    owner = load_owner_rules(_db_path(tmp_path))
    plan = plan_card(owner, include=[ids[PROJECT]], only=[ids[PROJECT]])
    assert [rule.id for rule in plan.selection.chosen] == [ids[PROJECT]]
    assert "Acme" in " ".join(plan.layouts[0].texts())


def test_only_naming_a_project_rule_without_the_flag_is_refused(
    tmp_path: Path, ids: dict[str, str]
) -> None:
    owner = load_owner_rules(_db_path(tmp_path))
    with pytest.raises(SelectionError, match="--include-project"):
        plan_card(owner, only=[ids[PROJECT]])


def test_exclusion_is_honoured_and_listed(tmp_path: Path, ids: dict[str, str]) -> None:
    owner = load_owner_rules(_db_path(tmp_path))
    gone = GENERAL[3]
    plan = plan_card(owner, exclude=[ids[gone]])
    assert ids[gone] not in plan.shown_ids()
    assert ids[gone] not in {rule.id for rule in plan.selection.chosen}
    for layout in plan.layouts:
        assert "tracking pixels" not in to_svg(layout)
    preview = "\n".join(preview_lines(plan))
    assert f"Excluded by you (1):\n  {ids[gone]}" in preview


def test_exclusion_wins_over_include_and_only() -> None:
    rules = [_rule(1, "No telemetry.", scope="acme"), _rule(2, "Ship it.")]
    selection = select(rules, include=[rules[0].id], only=[rules[0].id], exclude=[rules[0].id])
    assert selection.chosen == ()
    assert selection.excluded == (rules[0],)


def test_unknown_short_and_ambiguous_ids_are_errors() -> None:
    rules = [_rule(1, "One."), _rule(2, "Two.")]
    with pytest.raises(SelectionError, match="matches no rule"):
        select(rules, exclude=["01ZZZZZZ"])
    with pytest.raises(SelectionError, match="at least"):
        select(rules, exclude=["01T"])
    with pytest.raises(SelectionError, match="matches 2 rules"):
        select(rules, exclude=["01TEST"])


# -- composition and fitting -------------------------------------------------------------


def test_the_card_carries_only_the_kept_slots() -> None:
    rules = [
        _rule(1, "Prefer small pull requests."),
        _rule(2, "No analytics, no telemetry, no tracking pixels."),
        _rule(3, "Do not commit unless asked.", stated=3),
        _rule(4, "Never log a secret."),
        _rule(5, "Always write the test first."),
        _rule(6, "Never push on a Friday."),
        _rule(7, "Keep the README short."),
    ]
    plan = plan_card(_owner(*rules), handle="egor", measure=heuristic_measure)
    content = plan.content
    assert content.headline == HEADLINE == "My Way"
    assert content.strongest is not None and content.strongest.id == rules[1].id
    square, story = plan.layouts
    for layout in plan.layouts:
        texts = layout.texts()
        assert {"Varyz", "@egor", "My Way", "varyzai.com"} <= set(texts)
        assert content.verdict.archetype.title in texts
        assert content.verdict.archetype.line in texts
        # Cut on 2026-09-25: the "said it N times" panel, the grounding line
        # (it lives in the preview), the quote's label and the second stat.
        assert not any("SAID IT" in text for text in texts)
        assert content.verdict.evidence not in texts
        assert [text for text in texts if text.isupper()] == [LIST_LABEL.upper()]
        assert "7 rules on record" in texts
        assert not any("answered" in text for text in texts)
    assert [p.slot for p in square.placed] == ["strongest", "rule 1", "rule 2", "rule 3"]
    assert [p.slot for p in story.placed] == [
        "strongest", "rule 1", "rule 2", "rule 3", "rule 4", "rule 5",
    ]
    assert content.verdict.evidence in "\n".join(preview_lines(plan))


def test_the_strongest_opinion_prefers_a_short_firm_rule_that_fits() -> None:
    long_firm = _rule(
        1,
        "Every autonomous system gets a kill switch, a preflight doctor and an audit "
        "trail, without exception, however small the system and however trusted the "
        "operator, and nobody skips it.",
    )
    short_firm = _rule(2, "Never ship placeholders.")
    soft = _rule(3, "Prefer tabs.")
    content = compose([long_firm, short_firm, soft], quote_fits=lambda text: len(text) < 60)
    assert content.strongest == short_firm
    assert content.house_rules[0] == long_firm
    # Nothing firm fits: the firmest is used and shortened, never a softer rule.
    content = compose([long_firm, short_firm], quote_fits=lambda text: False)
    assert content.strongest == long_firm
    # A rule far softer than the firmest never takes the quote just for fitting.
    content = compose([long_firm, soft], quote_fits=lambda text: len(text) < 60)
    assert content.strongest == long_firm


def test_answers_print_only_when_they_read_as_a_rule() -> None:
    assert stands_alone("Commits use the imperative mood.")
    assert stands_alone("No analytics on any site.")
    for reply in (
        "None.", "Yes.", "No, use Plausible instead.", "It depends on the client.",
        "Same as the other site.", "Maybe the second one?", "use plausible for everything",
        "2 spaces, never tabs.",
    ):
        assert not stands_alone(reply), reply
    printed = _answer(1, "How should commits be written?", "Commits use the imperative mood.")
    question = _answer(2, "Which analytics should sites use?", "None.")
    assert card_text(printed) == "Commits use the imperative mood."
    assert card_text(question) is None
    assert card_text(_rule(3, "Why not ship on Fridays?")) is None
    plan = plan_card(_owner(printed, question, _rule(3, "Never log a secret.")))
    shown = " ".join(" ".join(layout.texts()) for layout in plan.layouts)
    assert "Commits use the imperative mood." in shown
    assert "?" not in shown and "None." not in shown
    assert plan.content.unprintable == (question,)
    preview = "\n".join(preview_lines(plan))
    expected = f"never printed: the answer needs its question to make sense (1):\n  {question.id}"
    assert expected in preview


def test_the_fixture_question_and_answer_never_reaches_the_card(
    tmp_path: Path, ids: dict[str, str]
) -> None:
    plan = plan_card(load_owner_rules(_db_path(tmp_path)))
    assert ids["answer"] not in plan.shown_ids()
    for layout in plan.layouts:
        assert "analytics should" not in to_svg(layout)


def test_a_card_of_only_questions_is_refused_rather_than_printed() -> None:
    with pytest.raises(EmptyCardError):
        plan_card(_owner(_answer(1, "Which analytics should sites use?", "None.")))


def test_handles_are_normalised_or_refused() -> None:
    assert normalize_handle("egor") == "@egor"
    assert normalize_handle(" @egor.m_2-x ") == "@egor.m_2-x"
    assert normalize_handle("") == ""
    assert normalize_handle("a" * 30) == "@" + "a" * 30
    for bad in ("@", "@@egor", "egor m", "<@egor & co>", "a" * 31, "egor/admin", "\u00e9gor"):
        with pytest.raises(HandleError):
            normalize_handle(bad)


def test_no_handle_leaves_the_corner_empty() -> None:
    plan = plan_card(_owner(_rule(1, "Never log a secret.")), measure=heuristic_measure)
    for layout in plan.layouts:
        assert not any(
            isinstance(element, Text) and element.anchor == "end" and element.y < 200
            for element in layout.elements
        )


def test_a_sparse_card_stays_a_compact_balanced_block() -> None:
    plan = plan_card(_owner(_rule(1, "Use British spelling in prose.")))
    for layout in plan.layouts:
        spec = SPECS[layout.size]
        texts = [element for element in layout.elements if isinstance(element, Text)]
        by_text = {element.text: element for element in texts}
        headline = by_text["My Way"]
        tagline = by_text[plan.content.verdict.archetype.line]
        (quote,) = [element for element in texts if element.text.startswith(OPEN_QUOTE)]
        (footer_line,) = [element for element in layout.elements if isinstance(element, Box)]
        above = headline.y - headline.size * 0.75 - by_text["Varyz"].y
        below = footer_line.y - quote.y
        assert 0.6 < above / below < 1.6, (layout.size, above, below)
        # Sections sit close: no empty band between the archetype and the quote.
        assert quote.y - tagline.y < 0.12 * layout.height, layout.size
        assert quote.size == spec.quote_size  # freed room never enlarges the type


def test_wrapped_lines_are_balanced_and_a_quote_mark_never_stands_alone() -> None:
    measure = lambda text: float(len(text))  # noqa: E731
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    greedy, _ = fit_lines(text, measure, 50, 2)
    balanced, cut = balanced_lines(text, measure, 50, 2)
    assert not cut and len(balanced) == len(greedy) == 2
    assert " ".join(balanced) == text
    assert len(greedy[-1]) < 10 <= len(balanced[-1])
    lines, cut = fit_lines(OPEN_QUOTE + "one two three", measure, 14, 2, closing=CLOSE_QUOTE)
    assert not cut and lines == [OPEN_QUOTE + "one two", "three" + CLOSE_QUOTE]


def test_long_rules_are_shortened_visibly_never_reworded() -> None:
    measure = lambda text: float(len(text))  # noqa: E731
    text = "one two three four five six seven eight nine ten"
    lines, truncated = fit_lines(text, measure, 12, 2)
    assert truncated
    assert lines[-1].endswith(ELLIPSIS)
    shown = " ".join(lines).removesuffix(ELLIPSIS)
    assert text.startswith(shown)
    assert fit_lines("short one", measure, 12, 2) == (["short one"], False)


def test_a_rule_too_long_for_the_card_ends_in_an_ellipsis() -> None:
    long_rule = _rule(1, "Never " + " ".join(f"word{n}" for n in range(200)) + ".")
    plan = plan_card(_owner(long_rule), measure=heuristic_measure)
    for layout in plan.layouts:
        (placed,) = [item for item in layout.placed if item.slot == "strongest"]
        assert placed.truncated
        drawn = " ".join(placed.lines)
        assert drawn.endswith(ELLIPSIS + "”")
        assert long_rule.text.startswith(drawn.removeprefix("“").removesuffix(ELLIPSIS + "”"))
    assert "(shortened)" in "\n".join(preview_lines(plan))


# -- output -----------------------------------------------------------------------------


def test_svg_is_well_formed_and_carries_the_brand(tmp_path: Path, ids: dict[str, str]) -> None:
    plan = plan_card(load_owner_rules(_db_path(tmp_path)), handle="egor")
    written = write_card(plan, tmp_path / "out", png=False)
    assert [path.name for path in written] == ["varyz-my-way-square.svg", "varyz-my-way-story.svg"]
    for path, (width, height) in zip(written, ((1080, 1080), (1080, 1920)), strict=True):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert (root.get("width"), root.get("height")) == (str(width), str(height))
        texts = [element.text for element in root.iter("{http://www.w3.org/2000/svg}text")]
        assert {"Varyz", "My Way", "varyzai.com", "@egor"} <= set(texts)
    # Markup in a rule is escaped, not interpreted.
    odd = plan_card(_owner(_rule(1, "Never mix <b> & <i> in one heading.")))
    root = ET.fromstring(to_svg(odd.layouts[0]))
    texts = [element.text for element in root.iter("{http://www.w3.org/2000/svg}text")]
    assert any(text and "<b> & <i>" in text for text in texts)


def test_png_is_written_at_both_sizes(tmp_path: Path, ids: dict[str, str]) -> None:
    image = pytest.importorskip("PIL.Image")
    plan = plan_card(load_owner_rules(_db_path(tmp_path)))
    written = write_card(plan, tmp_path / "out")
    sizes = {}
    for path in written:
        if path.suffix == ".png":
            with image.open(path) as picture:
                sizes[path.name] = picture.size
    assert sizes == {
        "varyz-my-way-square.png": (1080, 1080),
        "varyz-my-way-story.png": (1080, 1920),
    }


# -- the command ---------------------------------------------------------------------------


def test_dry_run_shows_everything_and_writes_nothing(
    tmp_path: Path, ids: dict[str, str]
) -> None:
    db = _db_path(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    out = tmp_path / "cards"
    code, output = _run(tmp_path, "--dry-run", "--out", str(out))
    assert code == 0, output
    assert "Dry run: nothing written." in output
    assert ids[GENERAL[3]] in output and ids[PROJECT] in output
    assert "Why this archetype:" in output
    assert not out.exists()
    assert not (tmp_path / "data" / "wrapped").exists()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_declining_the_prompt_writes_nothing(tmp_path: Path, ids: dict[str, str]) -> None:
    out = tmp_path / "cards"
    result = CliRunner().invoke(
        app, ["wrapped", "--out", str(out), "--config", str(_config(tmp_path))], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "Nothing written." in result.output
    assert not out.exists()


def test_the_command_writes_local_files_and_never_opens_a_socket(
    tmp_path: Path, ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("the card must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    out = tmp_path / "cards"
    code, output = _run(
        tmp_path, "--yes", "--out", str(out), "--handle", "egor",
        "--include-project", ids[PROJECT], "--exclude", ids[GENERAL[0]],
    )
    assert code == 0, output
    names = sorted(path.name for path in out.iterdir())
    expected_png = ["varyz-my-way-square.png", "varyz-my-way-story.png"]
    assert set(names) >= {"varyz-my-way-square.svg", "varyz-my-way-story.svg"}
    assert set(names) - {"varyz-my-way-square.svg", "varyz-my-way-story.svg"} <= set(expected_png)
    svg = (out / "varyz-my-way-square.svg").read_text(encoding="utf-8")
    assert "@egor" in svg
    assert "cloud API" not in svg


def test_the_command_refuses_unknown_ids_and_bad_sizes(
    tmp_path: Path, ids: dict[str, str]
) -> None:
    code, output = _run(tmp_path, "--dry-run", "--exclude", "01NOSUCHRULE")
    assert code == 2 and "matches no rule" in output
    code, output = _run(tmp_path, "--dry-run", "--size", "banner")
    assert code == 2 and "--size" in output
    code, output = _run(tmp_path, "--dry-run", "--handle", "egor & co")
    assert code == 2 and "not a handle" in output


def test_the_command_says_when_nothing_is_left(tmp_path: Path, ids: dict[str, str]) -> None:
    args = ["--dry-run"]
    for key, rule_id in ids.items():
        if key != PROJECT:
            args += ["--exclude", rule_id]
    code, output = _run(tmp_path, *args)
    assert code == 1 and "No rules left for the card" in output


def test_one_size_only(tmp_path: Path, ids: dict[str, str]) -> None:
    code, output = _run(tmp_path, "--dry-run", "--size", "story")
    assert code == 0, output
    assert "story card (1080x1920)" in output and "square card" not in output


def test_without_a_database_it_says_so_and_creates_nothing(tmp_path: Path) -> None:
    code, output = _run(tmp_path, "--dry-run")
    assert code == 1 and "No varyz database found" in output
    assert not _db_path(tmp_path).exists()


def test_the_png_is_painted_with_the_bundled_fonts_on_every_machine() -> None:
    from varyz.card.fonts import BUNDLED, ROLES, font_file

    for role in ROLES:
        path = font_file(role)
        assert path is not None and path.parent == BUNDLED, role
    assert (BUNDLED / "OFL-SourceSerif4.txt").is_file()
    assert (BUNDLED / "OFL-IBMPlexSans.txt").is_file()
