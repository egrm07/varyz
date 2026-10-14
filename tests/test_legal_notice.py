"""The section 7(b) notice is a licence obligation, so it is checked like code.

LICENSE.additional-terms does not ask for a notice to the same effect; it asks
for that notice, preserved and displayed. A paraphrase, a reflowed line or a
year quietly bumped in one file and not the other would leave the program
displaying something the licence does not name, which is the one failure mode
this term has. Both directions are pinned: the wording the licence requires
appears in the package, and the package's notice says nothing the licence has
not already committed to.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from varyz import LEGAL_NOTICE

ROOT = Path(__file__).resolve().parents[1]
TERMS = (ROOT / "LICENSE.additional-terms").read_text(encoding="utf-8")


def required_notice() -> str:
    """The indented block section 7(b) quotes, dedented as it would be shown."""

    after = TERMS.split("including any modified version:", 1)[1]
    block = after.split("\n\n", 2)[1]
    return textwrap.dedent(block).strip()


def test_the_licence_states_the_notice_as_two_lines():
    lines = required_notice().splitlines()
    assert len(lines) == 2, lines
    assert lines[0].startswith("Based on varyzai")
    assert lines[1].startswith("Source: ")


def test_the_package_displays_the_notice_the_licence_requires():
    assert required_notice() in LEGAL_NOTICE


def test_the_notice_opens_with_it_rather_than_burying_it():
    """Appropriate Legal Notices are displayed, and a reader stops reading."""

    assert LEGAL_NOTICE.startswith(required_notice())


def _flat(text: str) -> str:
    """Whitespace and commas only; the terms file wraps and the notice does not."""

    return " ".join(text.lower().replace(",", "").split())


def test_the_trademark_sentence_matches_the_term_that_permits_it():
    """7(e) grants no rights in the names and says a fork must rename.

    Not pinned verbatim: section 7(b) is the term that requires a notice to be
    preserved word for word, and this is 7(e).  What must not drift is the
    claim, so the sentence is compared with its wrapping and punctuation
    removed rather than character by character.
    """

    sentence = "a fork is free to take the code and must choose its own name"
    assert sentence in _flat(TERMS)
    assert sentence in _flat(LEGAL_NOTICE)


def test_the_notice_names_the_file_carrying_the_terms():
    assert "LICENSE.additional-terms" in LEGAL_NOTICE
    assert (ROOT / "LICENSE.additional-terms").exists()


def test_version_displays_it():
    """The term is satisfied by display, not by shipping a file."""

    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "varyz", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    # rich wraps at the console width, so compare on words rather than lines.
    shown = " ".join(done.stdout.split())
    for line in LEGAL_NOTICE.splitlines():
        if line.strip():
            assert " ".join(line.split()) in shown
