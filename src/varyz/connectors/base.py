"""The connector contract every source implements — Gmail, chat exports,
journal imports, manual capture, and the future audio pipeline alike.

discover() yields (item, cursor) pairs so the checkpoint advances only after
each item is durably ingested; a crash mid-sync resumes where it left off.
"""

from __future__ import annotations

import codecs
import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from varyz.domain import Cursor, NormalizedEntry, RawItem


def expand(path: str) -> Path:
    """`~/notes` and `%USERPROFILE%\\notes` are what people type.

    Neither exists as written, so without this they came back as "none of the
    configured paths exist" — an error that names the path and still leaves the
    user staring at a folder that is plainly there. `find_repos` already called
    `expanduser`, and could never receive a `~` to expand because the existence
    check above it had already rejected it.
    """
    return Path(os.path.expandvars(path)).expanduser()


class SkipItem(Exception):
    """A connector's way of saying an item it offered turns out to hold nothing.

    Some emptiness is only visible after extraction — an `index.html` that is
    all markup, a `Guidelines.md` somebody created and never wrote in, a PDF of
    scanned pages with no text layer. Discovery cannot see it without parsing
    every file twice, and normalize has to return something, so the entries got
    made: 26 of them in this corpus, each embedded as a single chunk holding
    its own filename and nothing else. `recall` drops an empty excerpt, so they
    could never be evidence — they only ever took a rank away from something
    that could.

    Deliberately not an error. An error is a file that might work next time and
    whose earlier entry deserves to survive it; this is the connector declining
    the item, which puts it exactly where a file the walk never offered sits.
    """


def decode_text(raw: bytes) -> str:
    """Bytes from somebody's text file, decoded without inventing damage.

    `raw.decode("utf-8", errors="replace")` is right about most files and
    silently wrong about the rest, and the rest are all Windows: a document
    saved as ANSI puts an em-dash at byte 0x97, which is not valid UTF-8, so
    every one became U+FFFD. Measured on the eval corpus, 6 of 786 documents
    carried 42 replacement characters between them — small, but each one sits
    in an embedded chunk and in the excerpt quoted back to the caller, and the
    original character is recoverable in every case.

    A BOM is believed before anything is guessed, because it is the file
    stating its own encoding; Notepad's "Unicode" writes UTF-16 LE with one.
    Then UTF-8 strictly, so a valid UTF-8 file can never be read as anything
    else. cp1252 is the fallback rather than latin-1 despite decoding nearly
    any byte: they differ exactly in 0x80–0x9F, which is where Windows keeps
    the smart quotes and dashes this exists to recover.
    """
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        # UTF-32 LE begins with the UTF-16 LE BOM followed by two zero bytes,
        # so it has to be tested first or it decodes as UTF-16 and NUL.
        if raw.startswith(bom):
            with contextlib.suppress(UnicodeDecodeError):
                return raw.decode(encoding)
    for encoding in ("utf-8", "cp1252"):
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode(encoding)
    return raw.decode("utf-8", errors="replace")


def _anchor(path: Path, base: str | None) -> Path:
    """A relative path in config.toml means relative to config.toml.

    `paths = ["./notes"]` resolved against whatever directory the process
    happened to start in: the archive's own folder when someone runs
    `varyz sync` beside their config, and somewhere else entirely when autosync
    runs at login or a client launches the MCP server. The same line named a
    different folder depending on who read it, and the second reader reported
    that the folder did not exist.
    """
    if base is None or path.is_absolute():
        return path
    return Path(base) / path


def is_path_pattern(spelling: str) -> bool:
    """Whether a configured root was typed as a wildcard rather than a path.

    Roots are literal: `paths = ["./docs/*.md"]` matches nothing and is reported
    like any other root that is not there. "Not on this machine" is true of the
    string and useless to the person who wrote it, who has the folder open.
    """
    return any(char in spelling for char in "*?[")


def missing_paths(options: dict[str, Any]) -> tuple[str, ...]:
    """Configured roots that are not on this machine, spelled as they were typed.

    Not an error -- resolve_paths refuses only when none of them exist, because
    a laptop may legitimately carry two machines' folders. It is what the sync
    summary needs in order not to describe a skipped folder as a clean run.
    """
    base = options.get("_base_dir")
    return tuple(
        spelling
        for spelling in (str(p).strip() for p in options.get("paths") or ())
        if spelling and not _anchor(expand(spelling), base).exists()
    )


def resolve_paths(options: dict[str, Any], connector_id: str) -> list[Path]:
    """The configured roots for a path-based connector, or an explanation.

    An approved connector with nothing to walk is always a mistake, and a
    silent one: left alone it reports "0 new, 0 unchanged, 0 errors" and reads
    as a clean sync of an empty folder. The usual causes are a misspelled key
    (`folders` for `paths`) and a path typed for the wrong machine, and neither
    is discoverable from a run that claims to have succeeded.

    A path that merely doesn't exist yet is not fatal on its own — a laptop may
    legitimately be configured for two machines' folders — but all of them
    missing means none of the configuration applies here.
    """
    configured = [str(p).strip() for p in options.get("paths", []) if str(p).strip()]
    if not configured:
        raise ValueError(
            f"no paths configured. Add paths = [\"~/path/to/folder\"] under "
            f"[connectors.{connector_id}] in config.toml"
        )
    base = options.get("_base_dir")
    resolved = [_anchor(expand(p), base) for p in configured]
    if all(not p.exists() for p in resolved):
        # The configured spelling, not the expanded one: that is what they have
        # to go and edit.
        hint = (
            ". Paths are literal folders, not wildcards -- name the folder itself"
            if any(is_path_pattern(spelling) for spelling in configured)
            else ""
        )
        raise ValueError(f"none of the configured paths exist: {', '.join(configured)}{hint}")
    return resolved


@runtime_checkable
class Connector(Protocol):
    connector_id: ClassVar[str]

    # Does `discover` yield everything this source currently holds, every run?
    #
    # True only for sources that re-enumerate from scratch, which is what makes
    # "in the index but not in this run's output" mean "deleted at the source".
    # A cursor-based connector yields only what is new since its checkpoint, so
    # the same absence means "already ingested" — the opposite conclusion from
    # the same evidence, which is why this is opt-in and defaults to False.
    enumerates_corpus: ClassVar[bool] = False

    # A connector that knows how much it has to read may also keep
    # `read_fraction`, a float from 0 to 1 updated as `discover` goes, which
    # the sync's bar shows (and times). Optional, so not declared here: a
    # Protocol member would make every connector without one fail the check.

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]: ...

    def normalize(self, item: RawItem) -> NormalizedEntry: ...


_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    _REGISTRY[cls.connector_id] = cls
    return cls


def get_connector(connector_id: str) -> Connector:
    try:
        return _REGISTRY[connector_id]()
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown connector '{connector_id}'. Known: {known}") from None


def all_connector_ids() -> list[str]:
    return sorted(_REGISTRY)
