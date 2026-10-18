"""Normalization golden tests for Gmail records."""

import datetime
import logging
from email.message import EmailMessage

import pytest

from varyz.connectors import gmail_imap
from varyz.connectors.gmail_imap import GmailImapConnector, _external_id, strip_html
from varyz.domain import Kind, RawItem


def _eml(subject="Dinner on Friday?", html=False, references=None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Anna Schmidt <anna@example.com>"
    msg["To"] = "egor@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Thu, 02 Jul 2026 18:24:00 +0200"
    msg["Message-ID"] = "<abc123@mail.example.com>"
    if references:
        msg["References"] = references
    if html:
        msg.set_content("fallback text")
        msg.add_alternative(
            "<html><head><style>p{color:red}</style></head>"
            "<body><p>Are we still on for <b>Friday</b>?</p><p>xx Anna</p></body></html>",
            subtype="html",
        )
    else:
        msg.set_content("Are we still on for Friday?\n\nxx Anna")
    return msg.as_bytes()


def _gmail_item(raw: bytes) -> RawItem:
    return RawItem(
        external_id=_external_id(raw, "egor@example.com", "All Mail", 1, 42),
        payload=raw,
        payload_ext=".eml",
        metadata={"internaldate": None},
    )


def test_email_normalize_plain():
    norm = GmailImapConnector().normalize(_gmail_item(_eml()))
    assert norm.kind == Kind.EMAIL
    assert norm.external_id == "<abc123@mail.example.com>"
    assert norm.title == "Dinner on Friday?"
    assert norm.occurred_start.isoformat() == "2026-07-02T18:24:00+02:00"
    assert "From: Anna Schmidt <anna@example.com>" in norm.body
    assert "still on for Friday" in norm.body
    assert norm.thread_id == "<abc123@mail.example.com>"  # thread of one


def test_email_normalize_prefers_plain_over_html():
    norm = GmailImapConnector().normalize(_gmail_item(_eml(html=True)))
    assert "fallback text" in norm.body


def test_email_thread_root_from_references():
    raw = _eml(references="<root@x.com> <mid@x.com>")
    norm = GmailImapConnector().normalize(_gmail_item(raw))
    assert norm.thread_id == "<root@x.com>"


def test_external_id_fallback_without_message_id():
    raw = b"From: x@y.com\r\nSubject: hi\r\n\r\nbody"
    assert _external_id(raw, "acc@gmail.com", "INBOX", 7, 99) == "acc@gmail.com/INBOX/7/99"


def test_strip_html_drops_style_keeps_text():
    text = strip_html(
        "<html><style>p{x}</style><script>evil()</script>"
        "<p>Hello&nbsp;world</p><br><div>second line</div></html>"
    )
    assert "Hello world" in text
    assert "second line" in text
    assert "evil" not in text and "p{x}" not in text


def test_gmail_incremental_sync_never_authorizes_pruning():
    assert GmailImapConnector.enumerates_corpus is False


class _FakeGmail:
    """The three IMAP calls discover makes, against a German mailbox.

    Gmail translates its special folders, so "[Gmail]/All Mail" -- the name
    `varyz connect gmail` writes into every config -- is not on this account.
    """

    FOLDERS = (
        ((rb"\HasNoChildren",), b"/", "INBOX"),
        ((rb"\All", rb"\HasNoChildren"), b"/", "[Gmail]/Alle Nachrichten"),
    )

    def __init__(self, *args, **kwargs):
        self.selected = []
        self.searched = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, account, password):
        pass

    def list_folders(self):
        return list(self.FOLDERS)

    def folder_exists(self, name):
        return name in {n for _flags, _delim, n in self.FOLDERS}

    def select_folder(self, folder, readonly=True):
        self.selected.append(folder)
        return {b"UIDVALIDITY": 1}

    def search(self, criteria):
        self.searched.append(criteria)
        return [1]

    def fetch(self, uids, what):
        if "RFC822.SIZE" in what:
            return {u: {b"RFC822.SIZE": 100} for u in uids}
        return {u: {b"BODY[]": _eml(f"msg {u}"), b"INTERNALDATE": None} for u in uids}


@pytest.fixture
def imap(monkeypatch):
    """Answer discover's two outside dependencies: the server and the keyring."""
    import imapclient

    clients = []

    def make(*args, **kwargs):
        client = _FakeGmail()
        clients.append(client)
        return client

    monkeypatch.setattr(imapclient, "IMAPClient", make)
    monkeypatch.setattr(gmail_imap.secrets, "get_secret", lambda name: "app-password")
    return clients


def _discover(options, imap):
    connector = GmailImapConnector()
    return list(connector.discover({"account": "me@gmail.com", **options}, None))


def test_a_mailbox_in_another_language_is_still_read(imap):
    """A German (or Russian, or Spanish) Gmail synced nothing, ever.

    `varyz connect gmail` writes folders = ["[Gmail]/All Mail"] for every
    account, and that folder only exists when the account is in English.
    Selecting it ended the sync with a raw IMAP protocol string. The RFC 6154
    special-use flags the server also sends are not translated.
    """
    items = _discover({}, imap)

    assert len(items) == 1
    assert imap[0].selected == ["[Gmail]/Alle Nachrichten"]


def test_one_folder_the_account_lacks_does_not_cost_the_others(imap, caplog):
    """A renamed label, or a name typed for the other account, ended the sync."""
    with caplog.at_level(logging.WARNING):
        items = _discover({"folders": ["[Gmail]/Typo", "INBOX"]}, imap)

    assert len(items) == 1
    assert imap[0].selected == ["INBOX"]
    assert "[Gmail]/Typo" in caplog.text
    assert "Traceback" not in caplog.text


def test_a_mailbox_with_none_of_the_configured_folders_says_what_it_has(imap):
    with pytest.raises(ValueError) as excinfo:
        _discover({"folders": ["Nope", "Also nope"]}, imap)

    message = str(excinfo.value)
    assert "connectors.gmail" in message
    assert "[Gmail]/Alle Nachrichten" in message  # what to write instead


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"since": "01/01/2020"}, "since"),
        ({"batch_size": "many"}, "batch_size"),
        ({"max_size_mb": 0}, "max_size_mb"),
    ],
)
def test_a_setting_that_is_not_what_it_claims_names_itself(imap, options, expected):
    """strptime and int() raise about format strings and literals.

    These settings are typed into config.toml by hand; the message has to say
    which line of it to fix.
    """
    with pytest.raises(ValueError) as excinfo:
        _discover(options, imap)

    message = str(excinfo.value)
    assert expected in message
    assert "connectors.gmail" in message


def test_a_first_sync_bounded_by_since_asks_the_server_for_it(imap):
    _discover({"since": "2026-01-01"}, imap)

    assert imap[0].searched == [["SINCE", datetime.date(2026, 1, 1)]]


def test_the_api_transport_refuses_the_same_unreadable_date():
    """auth = "google" takes a different route to the same mailbox.

    There the date was pasted into a Gmail search query -- after:01/01/2020 --
    which matches nothing and reports it as an empty mailbox.
    """
    connector = GmailImapConnector()
    options = {"account": "me@gmail.com", "auth": "google", "since": "01/01/2020"}
    with pytest.raises(ValueError, match="since"):
        list(connector.discover(options, None))
