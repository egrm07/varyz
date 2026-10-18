"""What kind of work a task is, so the owner's definition of done can be handed over.

A task that builds a website should arrive at the agent together with everything
the owner expects of *any* website of theirs: favicon, SEO tags, social image,
mobile review. Detection is deliberately plain: task wording plus a few files at
the project root. It decides which checklists to show, never how to build; a
missed domain costs a checklist the agent can still ask for, and an extra one
costs a few lines the agent marks "not applicable".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Domain:
    name: str
    title: str
    triggers: re.Pattern[str]
    # Possible owner choices in this kind of work, not a mandatory questionnaire.
    # The working agent checks task relevance and existing answers before asking.
    decision_points: tuple[str, ...]


def _words(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(alternatives) + r")(?![\w-])", re.I)


# Not when the word names a product being built: "the travel app", "a trip planner feature".
_PRODUCT = (
    r"(?! (?:planner |booking |planning )?(?:apps?|features?|websites?|sites?|platforms?"
    r"|startups?|api|modules?|pages?|screens?|tables?|services?)(?![\w-]))"
)


DOMAINS: tuple[Domain, ...] = (
    Domain(
        "website",
        "Website or web page",
        _words(
            r"web ?sites?", r"landing(?: page)?s?", r"web ?pages?", r"home ?page",
            # "Historic sites" on a trip are places, not web pages.
            r"(?<!historic )(?<!heritage )(?<!tourist )(?<!camping )(?<!archaeological )sites?",
            r"web ?app", r"front-?end", r"html", r"css", r"next\.?js", r"vite", r"astro",
            r"tailwind", r"portfolio", r"blog", r"seo", r"favicon", r"hero section",
            r"waitlist page", r"pricing page", r"privacy policy page",
        ),
        (
            "Which brand colours, fonts and wordmark does this product use?",
            "Where is this site hosted and on which domain?",
            "Does the owner want analytics on this site, and which kind?",
            "Which legal pages does this site need?",
            "What tone should the copy on this site have?",
        ),
    ),
    Domain(
        "python-tool",
        "Python tool or CLI",
        _words(
            # "Help me learn Python" is studying, not building a tool.
            r"(?<!learn )(?<!learning )python", r"cli", r"command[- ]line", r"typer",
            r"pyproject", r"uv", r"pip",
            r"pytest", r"script", r"package", r"library", r"fastapi", r"pydantic",
        ),
        (
            "What are the owner's preferred Python release and package manager for new tools?",
            "Where should this tool keep its state and secrets?",
            "Should this tool include dry-run operation and a diagnostic command?",
        ),
    ),
    Domain(
        "mobile-app",
        "Mobile app",
        _words(
            r"ios", r"android", r"expo", r"react native", r"mobile(?: app)?", r"iphone",
            r"app store", r"testflight", r"eas build", r"swiftui", r"play store",
        ),
        (
            "Which platforms does this app ship to first?",
            "How is this app versioned and released?",
            "Which billing or subscription provider does this app use?",
        ),
    ),
    Domain(
        "docs",
        "Documentation or report",
        _words(
            r"readme", r"docs?", r"documentation", r"write[- ]?up", r"report", r"changelog",
            r"guide", r"architecture doc", r"todo\.md", r"release notes",
        ),
        ("Who is this document for, and where will it be read?",),
    ),
    Domain(
        "video",
        "Video",
        _words(
            r"videos?", r"promo(?: video)?s?", r"footage", r"trailers?", r"shorts",
            r"reels?", r"screen ?recordings?", r"voice-?overs?", r"narrat(?:ion|ed|or)",
            r"remotion", r"b-?roll", r"demo video", r"tiktoks?",
        ),
        (
            "Whose voice narrates this video, and in which language?",
            "How long is this video, and in which aspect ratio?",
            "Which music fits this video, if any?",
            "What does the end card show?",
        ),
    ),
    Domain(
        "evaluation",
        "Evaluation or benchmark",
        _words(
            r"eval(?:uation|uate)?s?", r"benchmarks?", r"metrics?", r"accuracy",
            r"fidelity", r"held[- ]out", r"a/b tests?", r"measure(?:ment)?", r"baseline",
            r"precision", r"recall", r"ablation",
        ),
        (
            "What is the honest core metric for this, and what baseline must it beat?",
            "Which data is held out and must never be trained or tuned on?",
        ),
    ),
    Domain(
        "automation",
        "Automation or background job",
        _words(
            r"automat(?:e|ed|ion|ically)",
            # "Schedule a dinner with Sam" is an event, not a job.
            r"schedul(?:e|er|ed|ing)(?! (?:an?|the|my|our) (?:[\w-]+ )?(?:meeting|call|dinner"
            r"|lunch|breakfast|coffee|catch-?up|appointment|party|date|visit|interview)s?"
            r"(?![\w-]))",
            r"cron", r"watchdog",
            r"autonomous(?:ly)?", r"overnight", r"background (?:job|task|run)", r"daemon",
            r"bot", r"pipeline", r"recurring",
        ),
        (
            "How often should this run, and what may it do without asking?",
            "How should the owner be notified about this job?",
        ),
    ),
    Domain(
        "ai-integration",
        "LLM or AI integration",
        _words(
            r"llms?", r"openai", r"anthropic", r"claude api", r"gpt-?\d*", r"prompts?",
            r"embeddings?", r"rag", r"deepseek", r"ollama", r"model provider", r"ai features?",
            r"chatbot",
        ),
        (
            "Which model provider does the owner want here, and via OAuth or API key?",
            "What spend cap applies to this feature?",
            "May personal data from this feature reach a cloud model?",
        ),
    ),
    Domain(
        "data-connector",
        "Data connector or import",
        _words(
            r"connectors?", r"ingest(?:ion)?", r"import(?:er)? from", r"sync(?:ing)?",
            r"gmail", r"telegram", r"whatsapp", r"imap", r"webhooks?", r"scrap(?:e|er|ing)",
            r"export(?:ed)? data", r"integration with",
        ),
        (
            "Which account and date range should this connector read?",
            "What must be excluded or redacted before this data is used?",
        ),
    ),
    Domain(
        "dashboard",
        "Local dashboard or UI panel",
        _words(
            r"dashboards?", r"admin (?:panel|page|ui)", r"control panel", r"status page",
            r"web ui", r"local ui", r"control plane",
        ),
        ("Which port and which actions should this dashboard expose?",),
    ),
    Domain(
        "release",
        "Release, deploy or publish",
        _words(
            r"releas(?:e|ing)", r"ship(?:ping)?", r"deploy(?:ing|ment)?", r"publish(?:ing)?",
            r"launch(?:ing)?", r"version bump", r"go live", r"production", r"submission",
        ),
        (
            "Is this release allowed to go out without the owner's final look?",
            "Which version number and changelog entry should this release carry?",
        ),
    ),
    # Everyday work. Developer tasks say "message", "draft", "buy", "learn",
    # "post", "event" and "schedule" too, so these name the thing written,
    # bought or planned rather than a bare verb ("reply to my landlord's email",
    # not "reply"). A miss costs a checklist the agent can still ask for.
    Domain(
        "writing",
        "Writing or email",
        _words(
            r"(?:write|draft|compose|answer|reply to|respond to|proofread|rewrite) "
            # A determiner or an owner's name: "reply to messages" is a bot's job.
            r"(?:(?:an?|the|my|this|that|her|his|their|our) (?:[\w-]+'s )?|[\w-]+'s )"
            r"(?:e-?mails?|letters?|messages?|replies|reply|texts?|essays?|newsletters?"
            r"|bios?|speech(?:es)?|eulogy)",
            r"e-?mail (?:my|her|him|them|back)", r"essays?",
            r"thank[- ]you (?:notes?|letters?|cards?|e-?mails?)",
            r"(?:formal|polite|follow[- ]up|apology|complaint) (?:e-?mails?|letters?|messages?)",
            r"letters? of (?:recommendation|complaint|resignation)",
            r"(?:recommendation|resignation|complaint|motivation) letters?",
            r"out[- ]of[- ]office (?:reply|message|note)",
            r"(?:wedding|best man'?s?|maid of honou?r'?s?) speech(?:es)?",
        ),
        (
            "What tone does the owner take with this recipient?",
            "How does the owner open and sign off messages like this one?",
            "How long should this piece be?",
        ),
    ),
    Domain(
        "job-application",
        "Job application",
        _words(
            r"cover letters?", r"job applications?", r"résumés?", r"resumés?",
            # "Resume" alone is the download or the training run picking up again.
            r"(?:my|tailor(?:ed)? (?:my|the)) (?:cv|resume)s?",
            r"(?:cv|resume)s? for (?:an?|the|this|that) (?:[\w-]+ ){0,2}(?:job|role|position)",
            r"apply(?:ing)? (?:for|to) (?:an?|the|this|that) (?:[\w-]+ ){0,2}"
            r"(?:jobs?|roles?|positions?|internships?)",
            r"job (?:offers?|interviews?|hunt(?:ing)?|search|postings?|ads?|descriptions?)",
            r"interview prep(?:aration)?",
            r"prepare for (?:an?|the|my) (?:[\w-]+ ){0,2}interviews?",
            r"linkedin (?:profile|headline|summary|about)", r"salary (?:negotiation|expectations?)",
            r"recruiters?", r"internships?",
        ),
        (
            "Which version of the owner's CV fits this application?",
            "Which roles or companies has the owner ruled out?",
            "What salary expectations should be stated, if any?",
        ),
    ),
    Domain(
        "travel",
        "Travel",
        _words(
            # "A trip planner feature for the travel app" is software, not a trip.
            r"(?<!round )(?<!round-)trips?" + _PRODUCT, r"travel(?:l?ing)?" + _PRODUCT,
            r"(?<!in )flights?", r"hotels?",
            r"itinerar(?:y|ies)", r"vacations?", r"airbnbs?", r"packing lists?",
            r"pack for", r"getaways?", r"layovers?", r"sightseeing", r"airports?",
            r"jet ?lag", r"(?:summer|winter|family|beach|ski) holidays?",
            r"holiday (?:trips?|plans?|rentals?|homes?)", r"on holiday",
        ),
        (
            "What budget does the owner have for this trip?",
            "Which airlines, seats and kinds of hotel does the owner prefer?",
            "How full should each day of the itinerary be?",
        ),
    ),
    Domain(
        "shopping",
        "Shopping",
        _words(
            r"shopping", r"shopping lists?", r"wish ?lists?",
            # "Let users buy a subscription" is billing work in an app.
            r"buy(?:ing)? (?:an?|some|new|me|myself|her|him|them|my)(?!(?: [\w-]+)? "
            r"(?:subscriptions?|plans?|licen[cs]es?|tiers?|credits?|tokens?|seats?)(?![\w-]))",
            r"(?:which|what) (?:[\w-]+ ){0,3}should i (?:buy|get|order)",
            r"worth buying", r"gift ideas?", r"gifts? for",
            r"(?:an?|birthday|christmas|wedding|anniversary|housewarming) (?:gift|present)s?"
            r"(?! (?:subscriptions?|cards?|codes?))",
            r"compare (?:these|those|the) (?:[\w-]+ ){0,2}"
            r"(?:products|phones|laptops|brands|prices|deals|options to buy)",
            r"price comparison", r"(?:good|best|black friday|cyber monday) deals?",
            r"best (?:[\w-]+ ){0,2}(?:to buy|under \$?\d+|for the money)",
        ),
        (
            "What price ceiling does the owner have for this?",
            "Which brands does the owner trust or avoid?",
            "Where does the owner usually buy things like this?",
        ),
    ),
    Domain(
        "cooking",
        "Cooking or meals",
        _words(
            r"recipes?", r"cook(?:ing)?", r"meals?", r"meal ?(?:plans?|prep|planning)",
            # "Schedule a dinner with Sam" is an event; "cook for dinner" is a meal.
            r"(?<!schedule a )(?<!book a )(?<!plan a )dinners?(?! (?:with|party|parties"
            r"|reservations?|invit\w*))",
            r"lunch(?:es)?", r"breakfasts?", r"groceries", r"grocery (?:lists?|shop)",
            r"bak(?:e|ing) (?:a|an|some|bread|cookies|cakes?|pies?|muffins|brownies)",
            r"vegetarian", r"vegan", r"gluten[- ]free", r"dairy[- ]free", r"ingredients?",
            r"desserts?", r"snacks?",
        ),
        (
            "Which dietary restrictions or allergies apply?",
            "How much time and skill does the owner want to put into cooking this?",
            "How many portions, and for whom?",
        ),
    ),
    Domain(
        "social-post",
        "Social media post",
        _words(
            r"tweets?", r"tweeting", r"social (?:media|posts?)",
            r"(?:linkedin|instagram|insta|facebook|x|twitter|threads|tiktok|bluesky|mastodon"
            r"|reddit) (?:posts?|captions?|threads?|stor(?:y|ies)|reels?)",
            r"post (?:it |this |that )?(?:on|to) (?:linkedin|instagram|x|twitter|facebook"
            r"|threads|bluesky|reddit|mastodon|tiktok)",
        ),
        (
            "Which platforms does the owner post on?",
            "Does the owner use hashtags and emoji in posts?",
            "What voice does the owner post in?",
        ),
    ),
    Domain(
        "events",
        "Events or scheduling",
        _words(
            r"(?<!third )(?<!3rd )part(?:y|ies)", r"weddings?",
            r"birthdays?(?! (?:gifts?|presents?))",
            r"anniversar(?:y|ies)", r"baby showers?", r"rsvps?", r"guest ?lists?",
            r"(?:wedding|party|birthday|dinner) invit(?:es?|ations?)",
            r"send (?:out )?(?:the )?invitations",
            r"(?:plan|organi[sz]e|host|throw)(?:ing)? (?:an?|my|our) (?:[\w-]+ ){0,2}"
            r"(?:events?|dinners?|meetups?|reunions?|celebrations?|get-togethers?|gatherings?)",
            r"get-togethers?", r"reunions?", r"celebrations?",
            r"schedul(?:e|ing) (?:an?|the|our|my) (?:[\w-]+ )?(?:meeting|call|catch-?up|dinner"
            r"|lunch|breakfast|coffee|appointment|date|visit)s?",
            r"(?:my|our) calendar",
        ),
        (
            "Which times does the owner protect from bookings?",
            "How many guests does the owner like, and whom?",
            "How do the owner's invitations usually go out?",
        ),
    ),
    Domain(
        "study",
        "Study or learning",
        _words(
            r"(?:help me|i want to|i need to|teach me to|how do i|how can i) learn",
            r"teach me", r"explain (?:it |this |that )?like i'?m (?:five|5|a [\w-]+)", r"eli5",
            r"flash ?cards?", r"homework", r"tutor(?:ing)?",
            r"(?:my|an|the|final|midterm|upcoming|entrance|for) exams?",
            r"(?:take|taking|online|my|this) courses?", r"course ?work",
            r"stud(?:y|ying) for", r"study (?:plans?|guides?|notes|sessions?|schedule|groups?)",
            r"(?:help me|i need to|i want to|i have to) study", r"revis(?:e|ion) for",
            r"quiz me", r"lecture notes?", r"textbooks?", r"syllabus",
            r"practice (?:problems|questions|tests?|exams?)",
        ),
        (
            "How does the owner like things explained: examples first, or theory first?",
            "What level is the owner at in this subject?",
            "Which format helps the owner most: summaries, flashcards or worked problems?",
        ),
    ),
)

BY_NAME = {domain.name: domain for domain in DOMAINS}

_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"})


def _markers(cwd: Path) -> set[str]:
    """Kinds of work a project folder already is, from a handful of root files."""
    found: set[str] = set()
    try:
        names = {entry.name.casefold() for entry in cwd.iterdir()}
    except OSError:
        return found
    if names & {"pyproject.toml", "setup.py", "setup.cfg"}:
        found.add("python-tool")
    if "index.html" in names or names & {"astro.config.mjs", "next.config.js", "vite.config.ts"}:
        found.add("website")
    package: dict[str, object] = {}
    if "package.json" in names:
        try:
            loaded = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
            package = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            package = {}
    dependencies: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies |= {str(name).casefold() for name in value}
    if dependencies & {"expo", "react-native"}:
        found.add("mobile-app")
    elif dependencies & {"next", "vite", "astro", "gatsby", "@sveltejs/kit", "nuxt"}:
        found.add("website")
    return found


# "A landing page for my CLI": what follows "for" is the subject of the work, not
# more work. The phrase runs to the end of the clause or the next "and".
_SUBJECT = re.compile(
    r"(?<![\w-])(?:for|about|showcas(?:e|es|ing))(?![\w-])(.*?)(?=[.;!?]|\sand\s|$)",
    re.I | re.S,
)


@lru_cache(maxsize=256)
def _slug_pattern(slug: str) -> re.Pattern[str] | None:
    """A kind of work the owner named themselves ('cover-letter'), as task words.

    Hyphens read as spaces and a plain plural counts: 'trip' matches "trips",
    'party' matches "parties", 'cover-letter' matches "cover letter".
    """
    words = [word for word in re.split(r"[\s_-]+", slug.casefold()) if word]
    if not words:
        return None
    *head, last = words
    if len(last) > 2 and last.endswith("y") and last[-2] not in "aeiou":
        tail = re.escape(last[:-1]) + r"(?:y|ies)"
    else:
        tail = re.escape(last) + r"(?:e?s)?"
    return _words(r"[\s_-]+".join([*(re.escape(word) for word in head), tail]))


def domain_title(name: str) -> str:
    """A kind of work's title; the owner's own kinds read as their slug."""
    known = BY_NAME.get(name)
    return known.title if known else name.replace("-", " ").capitalize()


def detect_domains(
    text: str, cwd: Path | None = None, extra: Iterable[str] = ()
) -> tuple[str, ...]:
    """Domains named by the task, then any the project folder itself implies.

    ``extra`` are kinds of work the owner filed rules under that the table above
    does not know ('gardening'); one counts when the task names it as a word.

    A domain named only as the subject ("a website for our iOS app") is dropped
    when the task names other work outside that phrase; "write tests for the
    CLI" names nothing else, so the CLI stays.

    The folder only adds a domain when the task wording found none: "fix the
    typo in README" in a Python repo is docs work, not a Python-tool checklist.
    """
    text = text or ""
    kinds = [(domain.name, domain.triggers) for domain in DOMAINS]
    for slug in dict.fromkeys(item.strip().casefold() for item in extra):
        pattern = _slug_pattern(slug) if slug and slug not in BY_NAME else None
        if pattern is not None:
            kinds.append((slug, pattern))
    named = [(name, pattern) for name, pattern in kinds if pattern.search(text)]
    work = _SUBJECT.sub(" ", text)
    outside = [(name, pattern) for name, pattern in named if pattern.search(work)]
    if outside:
        named = outside
    if named or cwd is None:
        return tuple(name for name, _ in named)
    implied = _markers(cwd)
    return tuple(domain.name for domain in DOMAINS if domain.name in implied)


def decision_points(domains: Iterable[str]) -> list[dict[str, str]]:
    return [
        {"domain": name, "question": question}
        for name in domains
        if name in BY_NAME
        for question in BY_NAME[name].decision_points
    ]


__all__ = [
    "BY_NAME", "DOMAINS", "Domain", "decision_points", "detect_domains", "domain_title",
]
