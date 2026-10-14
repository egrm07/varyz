"""Gmail via IMAP + app password — no OAuth, no Takeout exports.

One-time setup: `varyz connect gmail` (stores the app password in the OS
credential store and writes the config section). Backfill and incremental
sync are the same code path: a per-folder (UIDVALIDITY, last UID) cursor
advances after every durably ingested message, so an interrupted first sync
of a large mailbox just resumes where it stopped.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

from varyz import secrets
from varyz.connectors.base import register
from varyz.connectors.documents import strip_html
from varyz.domain import Cursor, Kind, NormalizedEntry, RawItem

log = logging.getLogger(__name__)

DEFAULT_FOLDERS = ["[Gmail]/All Mail"]
DEFAULT_MAX_SIZE_MB = 10
DEFAULT_BATCH = 200

MSGID_RE = re.compile(r"<[^>]+>")

# Gmail names its special folders in the language of the account, so the
# "[Gmail]/All Mail" every config is written with does not exist on a mailbox
# set to German or Russian -- and selecting it fails the entire sync with a
# raw IMAP protocol string. The server also flags those same folders with the
# RFC 6154 special-use attributes, which are not translated, so the English
# name can be read as a request for the flag it names.
SPECIAL_USE = {
    "[Gmail]/All Mail": rb"\All",
    "[Gmail]/Sent Mail": rb"\Sent",
    "[Gmail]/Drafts": rb"\Drafts",
    "[Gmail]/Starred": rb"\Flagged",
    "[Gmail]/Spam": rb"\Junk",
    "[Gmail]/Trash": rb"\Trash",
}


def password_secret_name(account: str) -> str:
    return f"gmail_app_password:{account}"


@register
class GmailImapConnector:
    connector_id: ClassVar[str] = "gmail"
    # Gmail discovery is cursor-based.  An empty incremental run means there
    # are no new messages; it must never be interpreted as a full-mailbox
    # enumeration that authorizes pruning older entries.
    enumerates_corpus: ClassVar[bool] = False
    checkpoint: Cursor | None = None

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        # Checked before the transports split, because both read these and a
        # date the Gmail API cannot read becomes a search query that quietly
        # matches nothing rather than the bound the user asked for.
        since = _since_date(options)
        batch = int(_whole_number(options, "batch_size", DEFAULT_BATCH))
        if options.get("auth") == "google":
            from varyz.connectors.google_gmail import discover_gmail_api

            yield from discover_gmail_api(options, cursor, connector=self)
            return
        from imapclient import IMAPClient

        account = options.get("account")
        if not account:
            raise ValueError("gmail connector needs 'account' in [connectors.gmail]")
        password = secrets.get_secret(password_secret_name(account))
        if not password:
            raise ValueError(
                f"No app password stored for {account}. Run: varyz connect gmail"
            )

        folders = options.get("folders", DEFAULT_FOLDERS)
        max_bytes = int(_whole_number(options, "max_size_mb", DEFAULT_MAX_SIZE_MB) * 1024 * 1024)
        state: dict[str, Any] = dict(cursor.data) if cursor else {}

        with IMAPClient(options.get("host", "imap.gmail.com"), ssl=True) as client:
            client.login(account, password)
            synced = 0
            for wanted in folders:
                folder = _folder_on_this_account(client, wanted)
                if folder is None:
                    # One folder the account does not have must not cost the
                    # user the ones it does: a renamed label, or a name typed
                    # for the other account, ended the whole sync here.
                    log.warning("no folder %s on this account, so not synced", wanted)
                    continue
                synced += 1
                info = client.select_folder(folder, readonly=True)
                uidvalidity = int(info[b"UIDVALIDITY"])
                fstate = state.get(folder, {})
                if fstate.get("uidvalidity") != uidvalidity:
                    fstate = {"uidvalidity": uidvalidity, "last_uid": 0}
                last_uid = int(fstate.get("last_uid", 0))

                if last_uid:
                    uids = [u for u in client.search(["UID", f"{last_uid + 1}:*"])
                            if u > last_uid]
                elif since:
                    uids = client.search(["SINCE", since.date()])
                else:
                    uids = client.search(["ALL"])
                uids = sorted(uids)

                for start in range(0, len(uids), batch):
                    chunk = uids[start : start + batch]
                    sizes = client.fetch(chunk, ["RFC822.SIZE"])
                    fetchable = [
                        u for u in chunk
                        if sizes.get(u, {}).get(b"RFC822.SIZE", 0) <= max_bytes
                    ]
                    skipped = set(chunk) - set(fetchable)
                    for u in sorted(skipped):
                        log.warning("skipping oversized message uid=%s in %s", u, folder)
                    resp = client.fetch(fetchable, ["BODY.PEEK[]", "INTERNALDATE"]) \
                        if fetchable else {}
                    for uid in chunk:
                        data = resp.get(uid)
                        # Oversized/missing messages still advance the cursor.
                        fstate["last_uid"] = uid
                        state[folder] = fstate
                        if data is None:
                            continue
                        raw = data[b"BODY[]"]
                        internal = data.get(b"INTERNALDATE")
                        yield (
                            RawItem(
                                external_id=_external_id(raw, account, folder, uidvalidity, uid),
                                payload=raw,
                                payload_ext=".eml",
                                metadata={
                                    "internaldate": internal.isoformat() if internal else None,
                                },
                            ),
                            Cursor(data=state),
                        )

            if folders and not synced:
                # Every folder skipped is a sync that reports nothing new and
                # means nothing is being read at all. That is the config to
                # fix, so it is raised rather than logged, and it says what
                # the account actually has to choose from.
                raise ValueError(
                    "None of the folders in [connectors.gmail] are on "
                    f"{account}. It has: {_folder_names(client)}"
                )

    def normalize(self, item: RawItem) -> NormalizedEntry:
        msg: EmailMessage = email.message_from_bytes(
            item.payload_bytes(), policy=email.policy.default
        )
        subject = str(msg.get("Subject", "")).strip() or None
        occurred = _message_date(msg, item.metadata.get("internaldate"))
        body_text = _best_body(msg)
        headers = _header_block(msg)
        thread = _thread_id(msg)
        return NormalizedEntry(
            kind=Kind.EMAIL,
            external_id=item.external_id,
            title=subject,
            body=f"{headers}\n\n{body_text}".strip(),
            occurred_start=occurred,
            captured_at=occurred,
            thread_id=thread,
        )


def _since_date(options: dict[str, Any]) -> datetime | None:
    """The first-backfill bound, or say that what was typed is not a date.

    Written by hand into config.toml, where the date in the other order is the
    obvious mistake to make. Over IMAP it raised from strptime, naming the
    format string but not the setting; over the API it was pasted into a Gmail
    search query, which matches nothing and says so to nobody.
    """

    since = options.get("since")
    if not since:
        return None
    try:
        return datetime.strptime(str(since).strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise ValueError(
            f"since = {since!r} in [connectors.gmail] is not a date. "
            "Write it as YYYY-MM-DD."
        ) from None


def _whole_number(options: dict[str, Any], key: str, default: float) -> float:
    """Read a numeric setting, or say which one is not a number.

    int("many") and float("10 MB") raise messages about literals and bases
    that name neither the setting nor the file it was typed into.
    """

    raw = options.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{key} = {raw!r} in [connectors.gmail] is not a number."
        ) from None
    if value <= 0:
        raise ValueError(f"{key} in [connectors.gmail] must be greater than zero, not {raw!r}.")
    return value


def _folder_on_this_account(client: Any, wanted: str) -> str | None:
    """The name to select for `wanted`, or None if the account has no such folder.

    Usually `wanted` itself. When it is not there and it names one of Gmail's
    special folders, the account is answering in another language and the
    folder carrying the matching RFC 6154 flag is the same folder under its
    translated name.
    """

    try:
        if client.folder_exists(wanted):
            return wanted
        flag = SPECIAL_USE.get(wanted)
        if flag is None:
            return None
        for flags, _delimiter, name in client.list_folders():
            if flag in flags:
                log.info("reading %s as %s on this account", wanted, name)
                return str(name)
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("could not look up folder %s: %s", wanted, exc)
    return None


def _folder_names(client: Any) -> str:
    """The account's folder names, for an error message that can be acted on."""

    try:
        names = [str(name) for _flags, _delimiter, name in client.list_folders()]
    except (imaplib.IMAP4.error, OSError) as exc:  # pragma: no cover - server refused LIST
        return f"(the server would not list them: {exc})"
    return ", ".join(names[:40]) or "(none)"


def _external_id(raw: bytes, account: str, folder: str, uidvalidity: int, uid: int) -> str:
    """RFC Message-ID when present (stable across folders and refetches);
    positional fallback otherwise."""
    head = raw[:16384].decode("latin-1", errors="replace")
    m = re.search(r"^Message-ID:\s*(<[^>]+>)", head, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1)
    return f"{account}/{folder}/{uidvalidity}/{uid}"


def _message_date(msg: EmailMessage, internaldate: str | None) -> datetime | None:
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            dt = parsedate_to_datetime(str(date_hdr))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass
    if internaldate:
        dt = datetime.fromisoformat(internaldate)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _header_block(msg: EmailMessage) -> str:
    lines = []
    for name in ("From", "To", "Cc"):
        value = msg.get(name)
        if value:
            lines.append(f"{name}: {value}")
    return "\n".join(lines)


def _thread_id(msg: EmailMessage) -> str | None:
    """Root of the References chain groups a conversation; fall back to
    In-Reply-To, then the message's own id (a thread of one)."""
    refs = msg.get("References")
    if refs:
        ids = MSGID_RE.findall(str(refs))
        if ids:
            return ids[0]
    reply_to = msg.get("In-Reply-To")
    if reply_to:
        ids = MSGID_RE.findall(str(reply_to))
        if ids:
            return ids[0]
    own = msg.get("Message-ID")
    return str(own).strip() if own else None


def _best_body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = strip_html(content)
    return content.strip()

