"""The card's archetype: a short title computed from the rules, and why.

No model and no randomness. Every rule is checked against a fixed set of word
families ("signals"); a rule counts once towards each family it mentions, and a
rule filed under a kind of work (its ``domain``) counts once more towards the
family that work belongs to. The family most rules speak to names the
archetype, unless the two largest families run level; then the pairing of the
two names it (``PAIRS``, one title for every pair). The same rules always give the same archetype,
and ``explain`` states the counts and the rule that decided it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from varyz.card.source import CardRule


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    pattern: re.Pattern[str]
    domains: frozenset[str]


def _words(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(alternatives) + r")(?![\w-])", re.IGNORECASE)


# Listed in tie-break order: when two families have the same count, the earlier wins.
SIGNALS: tuple[Signal, ...] = (
    Signal(
        "guard", "privacy and permission",
        _words(
            r"privacy", r"private", r"secrets?", r"sensitive", r"fail[- ]closed",
            r"credentials?", r"passwords?", r"tokens?", r"encrypt\w*", r"consent",
            r"permission", r"telemetry", r"analytics", r"tracking", r"personal data",
            r"gdpr", r"leak\w*", r"local[- ]first", r"never logged", r"\.env",
        ),
        frozenset(),
    ),
    Signal(
        "proof", "proof and measurement",
        _words(
            r"tests?", r"pytest", r"measure\w*", r"metrics?", r"benchmarks?", r"baselines?",
            r"evals?", r"evaluation", r"held[- ]out", r"verif\w+", r"gates?", r"ruff",
            r"mypy", r"typecheck", r"lint\w*", r"numbers", r"replicat\w+", r"coverage",
            r"proof",
        ),
        frozenset({"evaluation"}),
    ),
    Signal(
        "pilot", "checks before acting",
        _words(
            r"dry[- ]run", r"preflight", r"doctor", r"confirm\w*", r"ask(?: first)?",
            r"review\w*", r"approv\w+", r"kill switch", r"rollbacks?", r"backups?",
            r"unless asked", r"before", r"audit\w*", r"rate caps?",
        ),
        frozenset(),
    ),
    Signal(
        "finish", "finishing and shipping",
        _words(
            r"done", r"finish\w*", r"ship\w*", r"release\w*", r"polish\w*", r"placeholders?",
            r"todo", r"stubs?", r"half[- ]wired", r"complete\w*", r"deploy\w*",
            r"definition of done",
        ),
        frozenset({"release"}),
    ),
    Signal(
        "autopilot", "running on its own",
        _words(
            r"automat\w+", r"unattended", r"autonom\w+", r"schedul\w+", r"background",
            r"daemon", r"overnight", r"watchdog", r"by itself", r"hands[- ]off",
            r"without me", r"cron", r"recurring",
        ),
        frozenset({"automation"}),
    ),
    Signal(
        "lean", "keeping it small",
        _words(
            r"simple", r"simplest", r"minimal\w*", r"small\w*", r"lean", r"fewer",
            r"less", r"stdlib", r"no (?:new )?dependenc\w+", r"dependenc\w+", r"short",
            r"delete", r"boring", r"one place",
        ),
        frozenset(),
    ),
    Signal(
        "craft", "how it looks and reads",
        _words(
            r"design\w*", r"colou?rs?", r"fonts?", r"typograph\w*", r"layout",
            r"visual\w*", r"screenshots?", r"brand\w*", r"copy", r"tone", r"wording",
            r"favicon", r"mobile", r"pixel\w*", r"spacing", r"docs?", r"readme",
        ),
        frozenset({"website", "dashboard", "docs", "mobile-app"}),
    ),
    Signal(
        "speed", "speed",
        _words(
            r"fast(?:er|est)?", r"quick(?:ly|er)?", r"asap", r"speed", r"rapid\w*",
            r"immediately", r"right away", r"straight away", r"don'?t wait",
            r"without waiting", r"iterat\w+", r"momentum", r"hurry",
        ),
        frozenset(),
    ),
    Signal(
        "money", "spending",
        _words(
            r"costs?", r"costly", r"budget\w*", r"spend\w*", r"spent", r"cheap\w*",
            r"expensive", r"frugal", r"free tier", r"credits", r"ai calls",
            r"api (?:calls|charges|costs?)", r"save money", r"wast\w+",
        ),
        frozenset(),
    ),
)
BY_KEY = {signal.key: signal for signal in SIGNALS}


@dataclass(frozen=True)
class Archetype:
    key: str
    title: str
    line: str


T = Archetype
ARCHETYPES: dict[str, Archetype] = {
    # One family stands out.
    "guard": T("guard", "The Bodyguard", "Nothing leaves without permission."),
    "proof": T("proof", "The Fact-Checker", "Show me the numbers."),
    "pilot": T("pilot", "The Pilot", "Checklist first, then takeoff."),
    "finish": T("finish", "The Closer", "Done means done."),
    "autopilot": T("autopilot", "The Autopilot", "Sets it up once, then walks away."),
    "lean": T("lean", "The Minimalist", "Fewer parts, fewer surprises."),
    "craft": T("craft", "The Art Director", "If it looks wrong, it is wrong."),
    "speed": T("speed", "The Speed Demon", "Fast now, perfect later."),
    "money": T("money", "The Penny Pincher", "Every token counts."),
    # Two families run level.
    "auditor": T("auditor", "The Auditor", "Trusts receipts, not promises."),
    "bouncer": T("bouncer", "The Bouncer", "Not on the list, not getting in."),
    "shipper": T("shipper", "The Paranoid Shipper", "Ships fast. Trusts nothing."),
    "nightguard": T("nightguard", "The Night Guard", "Runs all night. Leaks nothing."),
    "hermit": T("hermit", "The Hermit", "Small, local, off the grid."),
    "curator": T("curator", "The Curator", "Shows only what it chooses to."),
    "getaway": T("getaway", "The Getaway Driver", "Fast, and leaves no trace."),
    "banker": T("banker", "The Swiss Banker", "Discreet, and nothing wasted."),
    "scientist": T("scientist", "The Scientist", "Hypothesis, dry run, result."),
    "inspector": T("inspector", "The Inspector", "Green checks, then ship."),
    "madscientist": T("madscientist", "The Mad Scientist", "Tests all night, reports at dawn."),
    "engineer": T("engineer", "The Engineer", "Measure twice, cut the rest."),
    "perfectionist": T("perfectionist", "The Perfectionist", "Pixel-perfect, and proven."),
    "speedrunner": T("speedrunner", "The Speedrunner", "Fastest time, verified."),
    "accountant": T("accountant", "The Accountant", "Every number adds up."),
    "captain": T("captain", "The Captain", "Every release lands."),
    "director": T("director", "The Flight Director", "Automated, never unsupervised."),
    "surgeon": T("surgeon", "The Surgeon", "Small cuts, checked twice."),
    "editor": T("editor", "The Editor", "Nothing goes out unread."),
    "pitcrew": T("pitcrew", "The Pit Crew", "Checked, fuelled, gone in seconds."),
    "cfo": T("cfo", "The CFO", "No spend without sign-off."),
    "machine": T("machine", "The Machine", "Ships while you sleep."),
    "sniper": T("sniper", "The Sniper", "One shot, done."),
    "artisan": T("artisan", "The Artisan", "Finished means beautiful."),
    "sprinter": T("sprinter", "The Sprinter", "First to the finish line."),
    "contractor": T("contractor", "The Contractor", "On time, on budget."),
    "clockmaker": T("clockmaker", "The Clockmaker", "Few parts, runs forever."),
    "showrunner": T("showrunner", "The Showrunner", "Runs itself. Looks good doing it."),
    "rocket": T("rocket", "The Rocket", "Launch it and let it fly."),
    "thrifty": T("thrifty", "The Thrifty Robot", "Runs itself, on a budget."),
    "purist": T("purist", "The Purist", "Nothing extra. Nothing ugly."),
    "hacker": T("hacker", "The Hacker", "Less code, sooner."),
    "monk": T("monk", "The Monk", "Wants nothing it doesn't need."),
    "sketch": T("sketch", "The Sketch Artist", "Looks good, ships today."),
    "thrift": T("thrift", "The Thrift Shopper", "Looks expensive. Wasn't."),
    "hustler": T("hustler", "The Hustler", "Fast, cheap, out the door."),
    # No family reaches MIN_SIGNAL.
    "pragmatist": T("pragmatist", "The Wildcard", "Keeps the AI guessing."),
}

# Every pairing of two families has its own title. It applies when the two
# largest families run level: both reach MIN_SIGNAL and the smaller count is at
# least three quarters of the larger.
F = frozenset
PAIRS: dict[frozenset[str], str] = {
    F({"guard", "proof"}): "auditor", F({"guard", "pilot"}): "bouncer",
    F({"guard", "finish"}): "shipper", F({"guard", "autopilot"}): "nightguard",
    F({"guard", "lean"}): "hermit", F({"guard", "craft"}): "curator",
    F({"guard", "speed"}): "getaway", F({"guard", "money"}): "banker",
    F({"proof", "pilot"}): "scientist", F({"proof", "finish"}): "inspector",
    F({"proof", "autopilot"}): "madscientist", F({"proof", "lean"}): "engineer",
    F({"proof", "craft"}): "perfectionist", F({"proof", "speed"}): "speedrunner",
    F({"proof", "money"}): "accountant",
    F({"pilot", "finish"}): "captain", F({"pilot", "autopilot"}): "director",
    F({"pilot", "lean"}): "surgeon", F({"pilot", "craft"}): "editor",
    F({"pilot", "speed"}): "pitcrew", F({"pilot", "money"}): "cfo",
    F({"finish", "autopilot"}): "machine", F({"finish", "lean"}): "sniper",
    F({"finish", "craft"}): "artisan", F({"finish", "speed"}): "sprinter",
    F({"finish", "money"}): "contractor",
    F({"autopilot", "lean"}): "clockmaker", F({"autopilot", "craft"}): "showrunner",
    F({"autopilot", "speed"}): "rocket", F({"autopilot", "money"}): "thrifty",
    F({"lean", "craft"}): "purist", F({"lean", "speed"}): "hacker", F({"lean", "money"}): "monk",
    F({"craft", "speed"}): "sketch", F({"craft", "money"}): "thrift",
    F({"speed", "money"}): "hustler",
}
MIN_SIGNAL = 2


@dataclass(frozen=True)
class SignalCount:
    key: str
    label: str
    count: int
    rule_ids: tuple[str, ...]
    words: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    archetype: Archetype
    counts: tuple[SignalCount, ...]
    reason: str
    rules_considered: int
    # One short sentence for the card itself, naming the count behind the title.
    evidence: str = ""

    def explain(self) -> list[str]:
        """The decision in plain lines: the counts, then the rule that picked the title."""
        lines = [
            f"{self.archetype.title} - {self.archetype.line}",
            f"Computed from {self.rules_considered} rule"
            f"{'' if self.rules_considered == 1 else 's'} on this card's list "
            "(no model, no randomness).",
        ]
        for signal in self.counts:
            if signal.count == 0:
                continue
            words = ", ".join(signal.words[:6])
            lines.append(f"  {signal.count:>3}  {signal.label}  ({words})")
        lines.append(f"Why: {self.reason}")
        return lines


def _count(rules: Sequence[CardRule], signal: Signal) -> SignalCount:
    ids: list[str] = []
    words: list[str] = []
    # Oldest rule first, whatever order the rules came in, so the explanation
    # (not only the title) is the same for the same rules.
    for rule in sorted(rules, key=lambda item: (item.created_at, item.id)):
        found = [match.group(0).casefold() for match in signal.pattern.finditer(rule.text)]
        by_domain = rule.domain in signal.domains
        if not found and not by_domain:
            continue
        ids.append(rule.id)
        for word in found:
            if word not in words:
                words.append(word)
        if by_domain and f"[{rule.domain}]" not in words:
            words.append(f"[{rule.domain}]")
    return SignalCount(signal.key, signal.label, len(ids), tuple(ids), tuple(words))


def classify(rules: Sequence[CardRule]) -> Verdict:
    """Pick the archetype for ``rules``; the same rules always give the same answer."""
    counts = tuple(_count(rules, signal) for signal in SIGNALS)
    ranked = sorted(counts, key=lambda item: (-item.count, SIGNALS.index(BY_KEY[item.key])))
    top, second = ranked[0], ranked[1]
    if top.count < MIN_SIGNAL:
        return Verdict(
            ARCHETYPES["pragmatist"],
            counts,
            f"no family reaches {MIN_SIGNAL} rules, so no single streak stands out.",
            len(rules),
            "No single streak stands out: a bit of everything.",
        )
    third = ranked[2]
    if second.count >= MIN_SIGNAL and second.count * 4 >= top.count * 3:
        tie_note = (
            f" Tied with {third.label}; the fixed order of families picks the pair."
            if third.count == second.count
            else ""
        )
        return Verdict(
            ARCHETYPES[PAIRS[frozenset({top.key, second.key})]],
            counts,
            f"{top.label} ({top.count}) and {second.label} ({second.count}) run level "
            "(within a quarter of each other), so the pairing names the title." + tie_note,
            len(rules),
            f"{_rules(top.count)} about {top.label}, {second.count} about {second.label}.",
        )
    return Verdict(
        ARCHETYPES[top.key],
        counts,
        f"{top.label} is the largest family ({top.count} of {len(rules)} rules), well "
        f"ahead of {second.label} ({second.count}).",
        len(rules),
        f"{_rules(top.count)} about {top.label}.",
    )


def _rules(count: int) -> str:
    return f"{count} rules are" if count != 1 else "1 rule is"


__all__ = ["ARCHETYPES", "PAIRS", "SIGNALS", "Archetype", "SignalCount", "Verdict", "classify"]
