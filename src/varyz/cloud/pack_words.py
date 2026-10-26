"""How a task asks for a rule pack someone sent: "use egr's design pack".

Words only, no AI call. Varyz Cloud uses it to pick the packs a briefing carries and
the local Varyz uses it on the copies saved in shared-packs/ when Cloud cannot answer,
so both read a request the same way.
"""

from __future__ import annotations

import re

SUMMARY = 20  # Received packs listed in a briefing.
LABEL = (
    "Rule packs people sent the owner, used only because the task asks for them. The "
    "owner's current request comes first, then their own applicable rules and answers, "
    "then these secondary rules. Follow each pack's policy, attribute rules to the "
    "sender and never save them as the owner's own preferences."
)


def who(pack):
    """The sender, username first: "@egr (Egor)", or "@egr" when the name adds nothing."""
    name = pack.get("from") or ""
    handle = "@" + pack["creator"]
    return handle if name.casefold() in ("", pack["creator"].casefold()) else (
        handle + " (" + name + ")"
    )


_TOKEN = re.compile(r"@?[^\W_][\w'’-]*", re.UNICODE)
# Words that name a pack or someone's rules: "Egor's design pack", "egors rules".
_ASKING = {
    "rule", "rules", "preference", "preferences", "standard", "standards", "pack", "packs",
    "varyz", "guideline", "guidelines",
}
# Words that say to use something: "use Egor design", "with Egor's design".
_USING = {
    "use", "using", "apply", "applying", "follow", "following", "with", "per", "via",
    "according", "load", "turn",
}
# Words after a sender that say they sent it: "the pack Egor shared".
_SENT = {"sent", "shared", "gave", "made", "wrote"}
# A request not to use it: "don't use Egor's design pack", "without the design pack".
_NOT = {"don't", "dont", "do-not", "not", "never", "without", "stop", "instead", "skip", "ignore"}
# Words that join a list of senders: "Egor's, Ann's and Maria's design packs".
_JOINING = {"and", "or", "plus", "also", "both", "as", "well"}
# Words that ask for every pack that fits: "use all my design packs", "both design packs".
_EVERY = {
    "all", "every", "each", "both", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten",
}


def _tokens(text):
    """Lowercase words, each with whether it was possessive ("egor's", "egors'")."""
    words = []
    for raw in _TOKEN.findall(text.casefold()):
        word = raw.replace("’", "'")
        possessive = word.endswith(("'s", "'"))
        words.append((word.removesuffix("'s").rstrip("'"), possessive))
    return words


def _refused(words, index):
    return any(w in _NOT for w, _ in words[max(0, index - 3):index])


def _near(word, wanted):
    """Same word, its plural, or (for longer words) one typo away."""
    plurals = (wanted + "s", wanted + "es")
    if word == wanted or word in plurals or wanted in (word + "s", word + "es"):
        return True
    if min(len(word), len(wanted)) < 5 or abs(len(word) - len(wanted)) > 1:
        return False
    return _one_edit(word, wanted)


def _one_edit(x, y):
    if len(x) == len(y):
        diff = [i for i, (c, d) in enumerate(zip(x, y, strict=True)) if c != d]
        return len(diff) <= 1 or (
            len(diff) == 2 and diff[1] == diff[0] + 1
            and x[diff[0]] == y[diff[1]] and x[diff[1]] == y[diff[0]]
        )
    short, long = sorted((x, y), key=len)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def _sender_at(words, index, names):
    """How many words the sender's name takes at index (0 if absent), and possessive."""
    for name in names:
        parts = name.casefold().split()
        span = words[index:index + len(parts)]
        if len(span) < len(parts) or any(w.lstrip("@") != n for (w, _), n in zip(
            span[:-1], parts[:-1], strict=True
        )):
            continue
        last, possessive = span[-1]
        last = last.lstrip("@")
        if last == parts[-1]:
            return len(parts), possessive
        if last == parts[-1] + "s" and len(parts[-1]) > 2:
            return len(parts), True  # "egors design" is "egor's design".
    return 0, False


def _names(pack):
    return sorted({pack["creator"], pack["from"]}, key=len, reverse=True)


def _list_end(words, index, others):
    """Where a list of senders that goes on at index ends, and if its last is possessive."""
    possessive, end = False, index
    while index < len(words):
        if words[index][0] in _JOINING:
            index += 1
            continue
        size, owned = _sender_at(words, index, others)
        if not size:
            break
        index = end = index + size
        possessive = owned
    return end, possessive


def _list_start(words, index, others):
    """Where the list of senders that ends just before index starts."""
    start = scan = index
    while scan:
        if words[scan - 1][0] in _JOINING:
            scan -= 1
            continue
        back = next((scan - n for n in range(1, 4) if scan - n >= 0
                     and _sender_at(words, scan - n, others)[0] == n), None)
        if back is None:
            break
        start = scan = back
    return start


def named(task, pack, others=()):
    """Whether the task asks for this sender's rules or pack, by username or name.

    Forms: "@egor", "Egor's rules", "egors Varyz", "Egor's new design pack", "use Egor
    design", "with egors designs", "the design rules Egor sent", "the pack from Egor".
    others are the names of everyone who sent packs, so a list counts for each of
    them: "Egor's, Ann's and Maria's design packs", "the packs from Egor and Ann".
    A name alone, or "ask Egor about the design", is not a request.
    """
    words = _tokens(task)
    names = _names(pack)
    everyone = sorted({*names, *others}, key=len, reverse=True)
    topic = (pack.get("name") or "").split()
    for i, (word, _) in enumerate(words):
        if word.startswith("@") and word[1:] == pack["creator"].casefold():
            return True
        size, possessive = _sender_at(words, i, names)
        if not size:
            continue
        start = _list_start(words, i, everyone)
        end, last = _list_end(words, i + size, everyone)
        if _refused(words, start) or (start == i and _refused(words, i)):
            continue
        possessive = last if end > i + size else possessive
        after = [w for w, _ in words[end:end + 4]]
        before = [w for w, _ in words[max(0, start - 4):start]]
        # "Egor's rules", "egor varyz", "Egor's new design pack".
        reach = 4 if possessive else 1
        if any(w in _ASKING for w in after[:reach]):
            return True
        # "use Egor design", "with egors designs": a use word, then the pack's topic.
        if topic and any(w in _USING for w in before) and all(
            any(_near(w, t) for w in after[:len(topic) + (2 if possessive else 0)])
            for t in topic
        ):
            return True
        # "the design rules Egor sent", "the pack Egor shared", "rules from Egor".
        if after[:1] and after[0] in _SENT and any(w in _ASKING for w in before):
            return True
        if before[-1:] == ["from"] and any(w in _ASKING for w in before[-4:-1]):
            return True
    return False


def titled(task, pack):
    """Whether the task names the pack itself: "use the design pack", "designs packs".

    Returns "every" when it asks for all such packs: "use all my design packs",
    "both design packs", "the design packs".
    """
    topic = (pack.get("name") or "").split()
    if not topic:
        return False
    tokens = _tokens(task)
    words = [w for w, _ in tokens]
    for i in range(len(words) - len(topic)):
        if not all(_near(words[i + j], t) for j, t in enumerate(topic)) or words[
            i + len(topic)
        ] not in ("pack", "packs"):
            continue
        # "gregor's design pack" names someone else; "don't use the design pack" refuses.
        if i and tokens[i - 1][1] or _refused(tokens, i):
            return False
        every = words[i + len(topic)] == "packs" or any(
            w in _EVERY or w.isdigit() for w in words[max(0, i - 3):i]
        )
        return "every" if every else True
    return False


def _every_pack(words):
    """Whether the task asks for every pack: "use all my packs", "apply every rule pack"."""
    filler = {"my", "the", "of", "rule", "rules", "shared", "received", "sent", "these"}
    for i, word in enumerate(words):
        if word not in _EVERY:
            continue
        rest = words[i + 1:i + 5]
        end = next((n for n, w in enumerate(rest) if w in ("pack", "packs")), None)
        if end is not None and all(w in filler for w in rest[:end]) and not _refused(
            [(w, False) for w in words], i
        ):
            return True
    return False


def _fits(pack, words):
    topic = (pack.get("name") or "").split()
    return bool(topic) and all(any(_near(w, t) for w in words) for t in topic)


def activated(packs, task):
    """The packs this task asks for, and title-only matches from several senders.

    A pack never activates just because the task fits its topic: the person names
    the sender ("use Egor's design pack", "@egr's rules") or the pack ("design pack").
    """
    text = task or ""
    words = [w for w, _ in _tokens(text)]
    if _every_pack(words):
        return list(packs), []
    others = sorted({name for pack in packs for name in _names(pack)})
    asked = [pack for pack in packs if named(text, pack, others)]
    if asked:
        chosen = []
        for owner in dict.fromkeys(pack["owner"] for pack in asked):
            theirs = [pack for pack in asked if pack["owner"] == owner]
            fitting = [p for p in theirs if _fits(p, words)]
            chosen += fitting or theirs
        return chosen, []
    found = [(pack, titled(text, pack)) for pack in packs]
    matches = [pack for pack, how in found if how]
    every = any(how == "every" for _, how in found)
    # "web design pack" means the web design pack, not every design pack.
    longest = max((len(pack["name"].split()) for pack in matches), default=0)
    matches = [pack for pack in matches if len(pack["name"].split()) == longest]
    if len({pack["owner"] for pack in matches}) > 1 and not every:
        return [], matches
    return matches, []
