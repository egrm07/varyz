"""How much of a conversation a sentence about it carries verbatim.

The conversation pass exists so that what a chat history knows can be used
without the chat history being sent anywhere. That promise holds only while the
sentences the pass writes are its own. A model asked to summarise will
sometimes copy instead, and a copied sentence from a private conversation *is*
the private conversation, whichever table it now sits in.

So overlap is measured, not assumed: the longest run of consecutive words a
conclusion shares with the messages it read. A short run is ordinary and has to
be allowed -- a date, a name, a place, "the fourteenth of April" -- because a
conclusion that may not name anything says nothing. A long one is a quotation.

Measured over 2,832 conclusions of the real archive: p50 2 words, p90 4, p99 7,
max 16. The threshold sits just above p99, where the distribution has already
flattened, so it catches the copies without touching the ordinary sentences.
"""

from __future__ import annotations

import re

WORD = re.compile("[^a-z0-9]+")

# Words of consecutive overlap at which a conclusion is treated as carrying the
# conversation's own words rather than describing them.
QUOTE_WORDS = 8


def words(text: str) -> list[str]:
    return [part for part in WORD.split(text.lower()) if part]


def longest_shared_run(needle: list[str], haystack: list[str]) -> int:
    """Longest run of consecutive words appearing in both, by suffix extension.

    Quadratic in the pair, which is what it can afford to be: a conclusion is a
    sentence and a window is a few hundred words, and this runs once per
    conclusion as it is written, never on a request path.
    """
    if not needle or not haystack:
        return 0
    previous = [0] * (len(haystack) + 1)
    best = 0
    for word in needle:
        current = [0] * (len(haystack) + 1)
        for j, other in enumerate(haystack, start=1):
            if word == other:
                current[j] = previous[j - 1] + 1
                if current[j] > best:
                    best = current[j]
        previous = current
    return best


def quotes(conclusion: str, source: str, *, threshold: int = QUOTE_WORDS) -> bool:
    """Does this sentence carry the source's own words rather than describe it?"""
    return longest_shared_run(words(conclusion), words(source)) >= threshold
