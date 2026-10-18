"""Check every tracked file against the archive it was written about, before pushing.

Varyz is built by reading a real archive, and the work of building it -- reports,
measurements, A/B results, notes -- is written on the same machine that holds it.
That is how a quoted message body ends up committed: not by anyone deciding to
publish it, but because a measurement quoted what it measured and the measurement
was worth keeping.  The repository has no remote yet, so this is the last cheap
moment to find those.

Two passes, both offline:

*Archive quotation.*  Every tracked text file is cut into overlapping runs of
``--shingle`` words; the archive's own bodies and titles are streamed past them.
Eight consecutive words shared with a private message are not a coincidence, and
this finds them wherever they were pasted from -- an excerpt, a citation, a
prompt transcript, a fixture someone built from a real conversation.

The archive has read this repository -- its documents connector ingested the
source tree and its git connector the commit log -- so the repository matches
itself thousands of times over, and none of those matches are leaks.  Entries
whose own identifier names a file or a commit inside the repository root are
skipped, which leaves the sources a leak actually comes from: messages,
conversations, and documents belonging to everything else on the machine.

What remains is reported in two classes, because they are not the same finding.
A run shared with a message or a chat transcript is somebody's private words in
a public file, and every one of those is listed.  A run shared with a document
belonging to another project on this machine is source or notes that were never
meant to ship here -- that matters too, but in bulk, so those are summarised by
the project they came from.

*Machine and identity marks.*  Absolute paths under a home directory, e-mail
addresses, long digit runs, and private-key headers.  These say who ran the
build and on what, which is a smaller leak than the first pass finds but a much
easier one to make.

Findings are written in full to a gitignored file under ``data/`` and summarised
on stdout as counts and redacted shapes, because a report naming what leaked is
itself a thing that must not be published.

Usage:
    uv run --no-sync python scripts/audit_publication.py
    uv run --no-sync python scripts/audit_publication.py --gate
    uv run --no-sync python scripts/audit_publication.py --paths src tests
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from varyz.config import load_config  # noqa: E402

# Text the audit can read as text.  A binary file cannot be scanned this way and
# is reported as unscanned rather than skipped silently.
TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".py",
        ".txt",
        ".json",
        ".jsonl",
        ".toml",
        ".yaml",
        ".yml",
        ".cfg",
        ".ini",
        ".html",
        ".css",
        ".js",
        ".sh",
        ".ps1",
        ".sql",
        ".csv",
        ".rst",
        "",
    }
)

# The public corpus is published on purpose and is not the owner's archive; a
# match against it means the archive contains a public document, not that the
# repository contains a private one.
EXEMPT_PREFIXES = ("evals/public_corpus/",)

# Sources whose entries are somebody talking.  A run shared with one of these is
# reported line by line; everything else is reported in bulk.
CONVERSATIONAL = frozenset({"whatsapp", "telegram", "ai_conversations", "email", "gmail"})

# A run appearing in this many distinct tracked files is an idiom of the
# codebase -- a dataclass line, a licence sentence -- and matching one in the
# archive says only that the archive has read code too.  Dropped, and counted.
#
# Except against a conversational source.  Nobody says a dataclass line on
# WhatsApp, so a run matched there is a quotation however often the repository
# repeats it -- and a sentence copied into three test files is more exposed than
# one sitting in a single fixture, not less.  Suppressing those hid five real
# quotations, including two of the archive owner's own messages, behind the rule
# meant to hide boilerplate.
IDIOM_FILES = 3

WORD = re.compile(r"[0-9a-z']+")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
HOME_PATH = re.compile(r"(?i)(?:[A-Z]:\\Users\\|/home/|/Users/)([^\\/\s\"'<>|:*?]+)")
LONG_DIGITS = re.compile(r"(?<!\d)(?:\+?\d[\d ().-]{8,}\d)(?!\d)")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# A date is a long run of digits and is never the leak.  812 of the 957 digit
# runs in this repository on 2026-08-30 were ISO dates in reports and stamps,
# which is the whole reason nobody could read the pass: a check that reports
# 957 findings, 85% of them the date at the top of a report, is one that gets
# skimmed, and two real Telegram ids sat in that noise for months.
# Shorter than this and a coincidental match against the archive's identifiers
# says nothing.  Telegram user ids are ten digits; WhatsApp numbers are longer.
ARCHIVE_ID_DIGITS = 8

DATE_SHAPED = re.compile(r"^\D{0,2}(?:19|20)\d\d[-./ ]?[01]\d[-./ ]?[0-3]\d")


def origin_label(external_id: str | None, root: Path) -> str:
    """Where a document entry lives, named the way a report may name it.

    The folder a quotation came out of is the useful half -- it says which
    project was quoted -- and the rest of the path is the owner's machine, which
    a published report has no business carrying.  Anything outside the directory
    holding this repository collapses to one label for the same reason.
    """

    if not external_id:
        return "unknown"
    candidate = Path(external_id.rsplit("@", 1)[0] if "@" in external_id else external_id)
    if not candidate.is_absolute():
        return "unknown"
    try:
        relative = candidate.resolve().relative_to(root.parent)
    except (OSError, ValueError):
        return "elsewhere on this machine"
    return relative.parts[0] if relative.parts else "elsewhere on this machine"


def is_repository_copy(external_id: str | None, root: Path) -> bool:
    """Is this entry the archive's copy of a file or a commit in the repository?

    Documents are identified by their path and commits by ``<repo path>@<sha>``,
    so both answer to a prefix test.  Only the repository itself is excluded -- a
    document from another project on the same machine is exactly the kind of
    thing a report quotes without meaning to publish it.
    """

    if not external_id:
        return False
    candidate = external_id.rsplit("@", 1)[0] if "@" in external_id else external_id
    try:
        path = Path(candidate)
        # Absolute only.  A WhatsApp message is identified by "<number>@<id>"
        # and a Telegram one by "<chat>:<id>"; resolving either as a relative
        # path lands it under the working directory, which is this repository,
        # and the pass would skip every private message it exists to look for.
        if not path.is_absolute():
            return False
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    return resolved == root or root in resolved.parents


def is_seed_repository_copy(external_id: str | None, root: Path) -> bool:
    """Whether a document is from the preserved Varyz tree this pivot copied.

    ``MANIFEST.md`` declares the sibling ``varys`` checkout (the tree's name before
    the 2026-09-24 rename; the folder on disk still has it) as this repository's
    curated seed and rollback boundary. Matches against that exact tree are
    source lineage, not quotations from an unrelated private document.
    """

    if not external_id:
        return False
    candidate = external_id.rsplit("@", 1)[0] if "@" in external_id else external_id
    try:
        path = Path(candidate)
        if not path.is_absolute():
            return False
        seed = (root.parent / "varys").resolve()
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    return resolved == seed or seed in resolved.parents


def is_repository_work_session(source: str, external_id: str | None) -> bool:
    """Whether an archive row is provenance from a coding-agent work session.

    Varyz imports owner-authored turns from Codex and Claude Code as
    ``ai_conversations``. Those turns routinely contain the specification,
    patches and source being built, so treating every overlap as a private-chat
    quotation produces thousands of overlapping findings for the repository's
    own work product. General ChatGPT/Claude conversations do not get this
    exemption: only the two coding-session identifier namespaces do.
    """

    if source != "ai_conversations" or not external_id:
        return False
    normalized = external_id.casefold()
    return normalized.startswith(("codex:", "claude_code:"))


def shingles(text: str, size: int) -> list[tuple[int, str]]:
    """Overlapping runs of ``size`` words, with the offset each one starts at."""

    words = [(match.start(), match.group()) for match in WORD.finditer(text.lower())]
    if len(words) < size:
        return []
    return [
        (words[index][0], " ".join(word for _offset, word in words[index : index + size]))
        for index in range(len(words) - size + 1)
    ]


@dataclass
class FileFindings:
    path: str
    quoted: list[dict[str, Any]] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.quoted) + len(self.marks)


def tracked_files(paths: tuple[str, ...]) -> list[str]:
    # Desktop sandboxes and CI containers may check out the tree as a different
    # OS user. Trust this exact repository for this read-only inventory without
    # mutating the user's global git configuration.
    command = ["git", "-c", f"safe.directory={ROOT}", "ls-files", "-z", *paths]
    output = subprocess.run(
        command, cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout
    return [item for item in output.split("\0") if item]


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def redact(text: str, keep: int = 4) -> str:
    """Enough of a match to recognise it by, not enough to be the leak again."""

    words = text.split()
    if len(words) <= keep:
        return " ".join(words)
    return " ".join(words[:keep]) + f" [+{len(words) - keep} words]"


def scan_marks(path: str, text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if PRIVATE_KEY.search(line):
            findings.append({"kind": "private_key", "line": number, "shape": "PEM header"})
        for match in HOME_PATH.finditer(line):
            findings.append(
                {
                    "kind": "home_path",
                    "line": number,
                    "shape": f"{match.group(0)[: match.group(0).index(match.group(1))]}<user>",
                    "user": match.group(1),
                }
            )
        for match in EMAIL.finditer(line):
            local, _, domain = match.group(0).partition("@")
            findings.append(
                {
                    "kind": "email",
                    "line": number,
                    "shape": f"{local[:1]}***@{domain}",
                    "address": match.group(0),
                }
            )
        for match in LONG_DIGITS.finditer(line):
            if DATE_SHAPED.match(match.group(0)):
                continue
            digits = re.sub(r"\D", "", match.group(0))
            findings.append(
                {
                    "kind": "long_digits",
                    "line": number,
                    "shape": f"{len(digits)} digits ending {digits[-2:]}",
                    "value": match.group(0),
                }
            )
    return findings


def suppress_as_idiom(shingle: str, idioms: frozenset[str] | set[str], entry_source: str) -> bool:
    """Is this match boilerplate the repository repeats, rather than a quotation?

    Only ever true away from a conversational source.  Nobody says a dataclass
    line on WhatsApp, so a run matched against a message is a quotation however
    many files repeat it -- and a sentence pasted into three test files is more
    exposed than one sitting in a single fixture, not less.
    """

    return shingle in idioms and entry_source not in CONVERSATIONAL


def promote_archive_identifiers(
    findings: dict[str, FileFindings], entries_naming: Callable[[str], int]
) -> int:
    """Reclassify digit runs that name something the archive knows.

    A digit run matching an entry's identifier is not a long number, it is a
    handle for a real account or chat, and the quotation pass cannot see it
    because it is not made of words.
    """

    promoted = 0
    for file in findings.values():
        for mark in file.marks:
            if mark["kind"] != "long_digits":
                continue
            digits = re.sub(r"\D", "", mark.get("value") or "")
            if len(digits) < ARCHIVE_ID_DIGITS:
                continue
            count = entries_naming(digits)
            if count:
                mark["kind"] = "archive_identifier"
                mark["entries"] = count
                mark["shape"] = f"{len(digits)} digits ending {digits[-2:]}, in {count} entries"
                promoted += 1
    return promoted


ACCEPTED = ROOT / "publication_accepted.json"


def digest(text: str) -> str:
    """Name a finding without restating it.

    The baseline below is a tracked file in a repository that is about to be
    public, so it may not contain one word of what it accepts.  A hash of the
    matched text identifies the same finding across runs and line moves, and
    reveals nothing; the human-written reason is what makes it reviewable.
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def finding_key(path: str, item: dict[str, Any]) -> tuple[str, str, str]:
    matched = (
        item.get("text") or item.get("value") or item.get("address") or item.get("shape") or ""
    )
    return (path, item["kind"], digest(matched))


def load_accepted() -> dict[tuple[str, str, str], str]:
    """Findings a person has looked at and written down a reason for."""

    if not ACCEPTED.exists():
        return {}
    payload = json.loads(ACCEPTED.read_text(encoding="utf-8"))
    return {
        (item["path"], item["kind"], item["digest"]): item.get("reason", "")
        for item in payload.get("accepted", ())
    }


def is_gated(item: dict[str, Any], path: str = "") -> bool:
    """Is this a finding a person must sign off before the repository is pushed?

    The three classes the report already lists line by line: somebody's private
    words, a digit run that names a real account, and a private key.  Not the
    bulk match against another project's source on this machine -- there are
    tens of thousands of those, they are summarised by project on purpose, and a
    baseline holding one entry each would be a list nobody could read, which is
    the failure this gate already made once.

    Under `src/` the source of the entry stops mattering.  Twenty thousand
    quotations sit in docs, reports and fixtures, and nearly all of them are
    this repository matching the archive's own copy of itself; fourteen are in
    shipping source, which is few enough to read one by one.  Two of those
    fourteen are sentences out of the owner's documents, carried into an
    extraction prompt as examples -- private words in a file every user of this
    program installs, and until now not gated at all, because they arrived from
    a source nobody talks on.
    """

    if item["kind"] == "archive_quotation":
        if item.get("entry_source") in CONVERSATIONAL:
            return True
        return path.replace("\\", "/").startswith("src/")
    return item["kind"] in {"archive_identifier", "private_key"}


def unaccepted(
    findings: dict[str, FileFindings], accepted: dict[tuple[str, str, str], str]
) -> list[tuple[str, dict[str, Any]]]:
    """Every gated finding the baseline does not already account for.

    An entry whose reason is blank is not accepted.  A baseline that can be
    filled in without saying why is a list of things nobody looked at.
    """

    out: list[tuple[str, dict[str, Any]]] = []
    for path, file in sorted(findings.items()):
        for item in file.quoted + file.marks:
            if not is_gated(item, path):
                continue
            if not accepted.get(finding_key(path, item), "").strip():
                out.append((path, item))
    return out


def write_accepted(
    findings: dict[str, FileFindings], existing: dict[tuple[str, str, str], str]
) -> None:
    """Record what is here now, carrying forward every reason already given."""

    rows = []
    for path, file in sorted(findings.items()):
        for item in file.quoted + file.marks:
            if not is_gated(item, path):
                continue
            key = finding_key(path, item)
            rows.append(
                {
                    "path": key[0],
                    "kind": key[1],
                    "digest": key[2],
                    "entry_source": item.get("entry_source"),
                    "reason": existing.get(key, ""),
                }
            )
    ACCEPTED.write_text(
        json.dumps(
            {
                "note": (
                    "Findings of scripts/audit_publication.py that a person has looked at "
                    "and judged safe to publish, named by a hash so this file carries none "
                    "of what it accepts. An entry with an empty reason is not accepted and "
                    "--gate still fails on it."
                ),
                "accepted": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def shown_path(out: Path) -> Path:
    """The findings path as the reader knows it, without assuming it lives here."""
    try:
        return out.relative_to(ROOT)
    except ValueError:
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shingle", type=int, default=8, help="words per shared run")
    parser.add_argument(
        "--paths", nargs="*", default=(), help="limit to these pathspecs (default: all tracked)"
    )
    parser.add_argument(
        "--gate", action="store_true", help="exit non-zero when anything is found"
    )
    parser.add_argument(
        "--accept",
        action="store_true",
        help=f"rewrite {ACCEPTED.name} from what is found, keeping the reasons already written",
    )
    parser.add_argument(
        "--without-archive",
        action="store_true",
        help="scan for machine and identity marks only, without opening the archive",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()
    if args.accept and args.without_archive:
        # The baseline is rewritten from what this run found.  Written from a run
        # that never opened the archive, it would silently drop every quotation.
        parser.error("--accept needs the archive; it cannot be built from marks alone")

    files = tracked_files(tuple(args.paths))
    if not files:
        parser.error("No tracked files selected; refusing an empty publication audit")
    findings: dict[str, FileFindings] = {}
    unscanned: list[str] = []

    # Every shingle in the repository, mapped back to where it was written.  The
    # repository is small and the archive is not, so the index is built from the
    # repository and the archive is streamed past it once.
    index: dict[str, list[tuple[str, int]]] = defaultdict(list)
    newlines: dict[str, list[int]] = {}
    for name in files:
        if name.startswith(EXEMPT_PREFIXES):
            continue
        path = ROOT / name
        if path.suffix.lower() not in TEXT_SUFFIXES:
            unscanned.append(name)
            continue
        text = read_text(path)
        if text is None:
            unscanned.append(name)
            continue
        marks = scan_marks(name, text)
        if marks:
            findings.setdefault(name, FileFindings(name)).marks.extend(marks)
        newlines[name] = [match.start() for match in re.finditer(r"\n", text)]
        for offset, shingle in shingles(text, args.shingle):
            index[shingle].append((name, offset))

    def line_at(name: str, offset: int) -> int:
        return bisect_right(newlines.get(name, ()), offset - 1) + 1

    idioms = {
        shingle
        for shingle, sites in index.items()
        if len({site for site, _offset in sites}) >= IDIOM_FILES
    }

    # Opened read-only and without migrating: a compile may be writing to this
    # database right now, and an audit must never be the thing that changes it.
    config = load_config(args.config)
    conn = (
        None
        if args.without_archive
        else sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
    )

    # Two real Telegram ids -- a group in 2,114 entries and a person in 261 --
    # were sitting in a committed test file until this asked.  Cheap: a handful
    # of candidates survive the date filter, one indexed LIKE each.
    promoted = (
        0
        if conn is None
        else promote_archive_identifiers(
            findings,
            lambda digits: conn.execute(
                "select count(*) from entries where external_id like ?", (f"%{digits}%",)
            ).fetchone()[0],
        )
    )

    seen: set[tuple[str, str]] = set()
    suppressed = 0
    entries_scanned = 0
    entries_skipped = 0
    work_sessions_skipped = 0
    seed_entries_skipped = 0
    rows = (
        ()
        if conn is None
        else conn.execute(
            "select id, source, external_id, title, body from entries "
            "where body is not null or title is not null"
        )
    )
    for entry_id, entry_source, external_id, title, body in rows:
        if is_repository_copy(external_id, ROOT):
            entries_skipped += 1
            continue
        if is_seed_repository_copy(external_id, ROOT):
            seed_entries_skipped += 1
            continue
        if is_repository_work_session(entry_source, external_id):
            work_sessions_skipped += 1
            continue
        entries_scanned += 1
        for field_text in (title, body):
            if not field_text:
                continue
            for _offset, shingle in shingles(field_text, args.shingle):
                if suppress_as_idiom(shingle, idioms, entry_source):
                    suppressed += 1
                    continue
                for name, position in index.get(shingle, ()):
                    if (name, shingle) in seen:
                        continue
                    seen.add((name, shingle))
                    findings.setdefault(name, FileFindings(name)).quoted.append(
                        {
                            "kind": "archive_quotation",
                            "offset": position,
                            "line": line_at(name, position),
                            "entry_id": entry_id,
                            "entry_source": entry_source,
                            "entry_origin": origin_label(external_id, ROOT),
                            "shape": redact(shingle),
                            "text": shingle,
                        }
                    )
    if conn is not None:
        conn.close()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.json_out or (ROOT / "data" / "audit" / f"publication-{stamp}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": stamp,
        "shingle_words": args.shingle,
        "files_scanned": len(files) - len(unscanned),
        "files_unscanned": unscanned,
        "entries_scanned": entries_scanned,
        "entries_skipped_as_repository_copies": entries_skipped,
        "entries_skipped_as_repository_work_sessions": work_sessions_skipped,
        "entries_skipped_as_seed_repository_copies": seed_entries_skipped,
        "idiom_runs": len(idioms),
        "matches_suppressed_as_idiom": suppressed,
        "digit_runs_naming_archive_entries": promoted,
        "archive_compared": conn is not None,
        "findings": {
            name: {"quoted": item.quoted, "marks": item.marks}
            for name, item in sorted(findings.items())
        },
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    newline = chr(10)
    if conn is None:
        print(
            f"scanned {payload['files_scanned']} tracked files for machine and identity "
            "marks only. The archive was not opened, so nothing here says whether the "
            "repository quotes it."
        )
    else:
        print(
            f"scanned {payload['files_scanned']} tracked files against "
            f"{entries_scanned} entries"
        )
        print(f"  skipped: {entries_skipped} entries that are this repository, read back")
        print(
            f"  skipped: {work_sessions_skipped} owner turns from Codex/Claude Code "
            "repository work sessions"
        )
        print(f"  skipped: {seed_entries_skipped} entries from the declared Varyz seed tree")
        print(
            f"  suppressed: {suppressed} non-conversational matches of {len(idioms)} runs "
            f"appearing in {IDIOM_FILES} or more tracked files"
        )
    if promoted:
        print(f"  digit runs that name an archive entry: {promoted}")
    if unscanned:
        print(f"  not scanned as text: {len(unscanned)}")
    if not findings:
        print("no archive quotation and no machine marks in tracked files")
        print(f"{newline}full findings (gitignored): {shown_path(out)}")
        return 0

    spoken = {
        name: [item for item in file.quoted if item["entry_source"] in CONVERSATIONAL]
        for name, file in findings.items()
    }
    spoken = {name: items for name, items in spoken.items() if items}
    total_spoken = sum(len(items) for items in spoken.values())

    if conn is not None:
        print(
            f"{newline}somebody's words, in {len(spoken)} tracked files "
            f"({total_spoken} runs)"
        )
    for name, items in sorted(spoken.items(), key=lambda pair: -len(pair[1])):
        sources = Counter(item["entry_source"] for item in items)
        named = "/".join(f"{key} x{count}" for key, count in sources.most_common())
        print(f"  {name}: {named}")
        shown: set[int] = set()
        for item in sorted(items, key=lambda item: item["line"]):
            if item["line"] in shown:
                continue
            if len(shown) == 6:
                print(f"      ... and {len(items) - 6} more run(s)")
                break
            shown.add(item["line"])
            print(f"      line {item['line']}: {item['shape']}")

    borrowed = [
        item
        for file in findings.values()
        for item in file.quoted
        if item["entry_source"] not in CONVERSATIONAL
    ]
    if borrowed:
        origins = Counter(item["entry_origin"] for item in borrowed)
        print(f"{newline}other work on this machine, quoted ({len(borrowed)} runs)")
        for origin, count in origins.most_common():
            print(f"  {origin}: {count}")
        per_file = Counter(
            name
            for name, file in findings.items()
            for item in file.quoted
            if item["entry_source"] not in CONVERSATIONAL
        )
        for name, count in per_file.most_common(8):
            named = "/".join(
                sorted(
                    {
                        item["entry_origin"]
                        for item in findings[name].quoted
                        if item["entry_source"] not in CONVERSATIONAL
                    }
                )
            )
            print(f"      {name}: {count} from {named}")

    marks = Counter(item["kind"] for file in findings.values() for item in file.marks)
    if marks:
        print(f"{newline}machine and identity marks")
        for kind, count in marks.most_common():
            print(f"  {kind}: {count}")
        ranked = sorted(findings.items(), key=lambda pair: -len(pair[1].marks))
        for name, file in ranked[:8]:
            if not file.marks:
                break
            detail = Counter(item["kind"] for item in file.marks)
            named = ", ".join(f"{kind} x{count}" for kind, count in detail.most_common())
            print(f"      {name}: {named}")

    print(f"{newline}full findings (gitignored): {shown_path(out)}")
    accepted = load_accepted()
    outstanding = unaccepted(findings, accepted)
    if args.accept:
        write_accepted(findings, accepted)
        print(
            f"{newline}{ACCEPTED.name}: {len(findings)} file(s) listed. "
            "Every new entry needs a reason written into it before it counts as accepted."
        )
        return 0

    print(f"{newline}not accounted for in {ACCEPTED.name}: {len(outstanding)}")
    for path, item in outstanding[:12]:
        print(f"  {path}:{item['line']}  {item['kind']}  {item.get('shape', '')}")
    if len(outstanding) > 12:
        print(f"  ... and {len(outstanding) - 12} more")

    if args.gate:
        return 1 if outstanding else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
