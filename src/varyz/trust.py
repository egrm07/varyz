"""Interactive MVP trust center for source, graph, privacy, and deletion controls."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from varyz import connectors as connector_registry
from varyz.autosync import _safe_error, is_enabled, is_paused, load_state, pause, resume
from varyz.config import load_config
from varyz.configfile import upsert_connector
from varyz.db import dao
from varyz.db.connection import WriteLock
from varyz.deletion import forget_entry
from varyz.domain import Sensitivity, local_day, local_stamp, new_id
from varyz.graph import builder_is_shipped
from varyz.graph.overrides import append_override
from varyz.pipeline.embed import Embedder, embed_pending
from varyz.pipeline.ingest import sync_connector
from varyz.privacy.doctor import diagnose as diagnose_privacy
from varyz.privacy.receipts import ReceiptStore
from varyz.resolution.models import Caller
from varyz.vault import Vault

Prompt = Callable[[str], str]
Confirm = Callable[[str], bool]


DEFAULT_AGENT_PURPOSE = "owner_context"
DEFAULT_DEVICE_ID = "local"
DEFAULT_AGENT_MAX_ITEMS = 8
DEFAULT_AGENT_MAX_CHARS = 4_000
LOW_TRUST_MAX_ITEMS = 1
LOW_TRUST_MAX_CHARS = 1_000
TOKEN_BYTES = 32


class AgentAuthenticationError(ValueError):
    """A supplied credential existed but is no longer valid."""


@dataclass(frozen=True)
class AgentCredential:
    """The one-time plaintext token and the durable identity it authenticates."""

    caller: Caller
    token: str
    token_id: str


def _utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def hash_agent_token(token: str) -> str:
    """Hash an opaque agent token; plaintext credentials are never persisted."""
    if not token or not token.strip():
        raise ValueError("agent token cannot be blank")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _manifest_hash(manifest: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def register_agent_principal(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    display_name: str,
    publisher: str = "",
    manifest: dict[str, object] | None = None,
) -> str:
    """Register or refresh one named principal without creating duplicates."""
    agent_id = agent_id.strip()
    display_name = display_name.strip()
    if not agent_id or not display_name:
        raise ValueError("agent id and display name cannot be blank")
    manifest_json, manifest_hash = _manifest_hash(
        manifest or {"agent_id": agent_id, "display_name": display_name, "publisher": publisher}
    )
    now = _utc_iso()
    conn.execute(
        """INSERT INTO agent_principals
           (id, display_name, publisher, trust_state, manifest_json, manifest_hash,
            created_at, revoked_at)
           VALUES (?, ?, ?, 'active', ?, ?, ?, NULL)
           ON CONFLICT(id) DO UPDATE SET
             display_name=excluded.display_name,
             publisher=excluded.publisher,
             manifest_json=excluded.manifest_json,
             manifest_hash=excluded.manifest_hash""",
        (agent_id, display_name, publisher, manifest_json, manifest_hash, now),
    )
    conn.commit()
    return agent_id


def mint_agent_token(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    label: str = "default",
    valid_for: timedelta = timedelta(days=365),
) -> tuple[str, str]:
    """Mint a bearer credential, returning plaintext exactly once and its row id."""
    principal = conn.execute(
        "SELECT trust_state, revoked_at FROM agent_principals WHERE id=?", (agent_id,)
    ).fetchone()
    if principal is None:
        raise ValueError(f"unknown agent principal: {agent_id}")
    if principal[0] != "active" or principal[1] is not None:
        raise AgentAuthenticationError(f"agent principal is revoked: {agent_id}")
    token = secrets.token_urlsafe(TOKEN_BYTES)
    token_id = new_id()
    now = datetime.now(UTC)
    conn.execute(
        """INSERT INTO agent_tokens
           (id, agent_id, label, token_hash, valid_until, revoked_at, created_at, last_used_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)""",
        (
            token_id,
            agent_id,
            label.strip() or "default",
            hash_agent_token(token),
            _utc_iso(now + valid_for),
            _utc_iso(now),
        ),
    )
    conn.commit()
    return token, token_id


def grant_agent_capability(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    purpose: str = DEFAULT_AGENT_PURPOSE,
    resource_types: tuple[str, ...] = ("summary", "owner_knowledge"),
    tool_names: tuple[str, ...] = ("resolve",),
    destinations: tuple[str, ...] = ("agent",),
    max_items: int = DEFAULT_AGENT_MAX_ITEMS,
    max_chars: int = DEFAULT_AGENT_MAX_CHARS,
    allow_background: bool = False,
    valid_for: timedelta = timedelta(days=365),
) -> str:
    """Create or reuse the active least-privilege context capability."""
    purpose = purpose.strip()
    if not purpose:
        raise ValueError("capability purpose cannot be blank")
    if max_items < 0 or max_chars < 0:
        raise ValueError("capability limits cannot be negative")
    now = datetime.now(UTC)
    encoded_resources = json.dumps(sorted(set(resource_types)))
    encoded_tools = json.dumps(sorted(set(tool_names)))
    encoded_destinations = json.dumps(sorted(set(destinations)))
    existing = conn.execute(
        """SELECT id FROM agent_capabilities
           WHERE agent_id=? AND effect_kind='context' AND purpose=?
             AND resource_types_json=? AND tool_names_json=? AND destinations_json=?
             AND max_items=? AND max_chars=? AND allow_background=?
             AND revoked_at IS NULL AND valid_until>?
           ORDER BY created_at DESC LIMIT 1""",
        (
            agent_id,
            purpose,
            encoded_resources,
            encoded_tools,
            encoded_destinations,
            max_items,
            max_chars,
            int(allow_background),
            _utc_iso(now),
        ),
    ).fetchone()
    if existing is not None:
        return str(existing[0])
    capability_id = new_id()
    conn.execute(
        """INSERT INTO agent_capabilities
           (id, agent_id, effect_kind, purpose, resource_types_json, tool_names_json,
            destinations_json, max_items, max_chars, allow_background, valid_until,
            revoked_at, created_at)
           VALUES (?, ?, 'context', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
        (
            capability_id,
            agent_id,
            purpose,
            encoded_resources,
            encoded_tools,
            encoded_destinations,
            max_items,
            max_chars,
            int(allow_background),
            _utc_iso(now + valid_for),
            _utc_iso(now),
        ),
    )
    conn.commit()
    return capability_id


def register_agent(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    display_name: str,
    purpose: str = DEFAULT_AGENT_PURPOSE,
    device_id: str = DEFAULT_DEVICE_ID,
    publisher: str = "",
    manifest: dict[str, object] | None = None,
) -> AgentCredential:
    """Register, grant and mint the credential used by one agent connection."""
    register_agent_principal(
        conn,
        agent_id=agent_id,
        display_name=display_name,
        publisher=publisher,
        manifest=manifest,
    )
    capability_id = grant_agent_capability(conn, agent_id, purpose=purpose)
    token, token_id = mint_agent_token(conn, agent_id)
    return AgentCredential(
        caller=Caller(
            agent_id=agent_id,
            display_name=display_name,
            purpose=purpose,
            capability_id=capability_id,
            device_id=device_id,
            transport="stdio",
        ),
        token=token,
        token_id=token_id,
    )


def _capability_for(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    purpose: str,
    tool_name: str,
) -> sqlite3.Row | tuple | None:
    now = _utc_iso()
    rows = conn.execute(
        """SELECT * FROM agent_capabilities
           WHERE agent_id=? AND effect_kind='context' AND purpose=?
             AND revoked_at IS NULL AND valid_until>?
           ORDER BY created_at DESC""",
        (agent_id, purpose, now),
    ).fetchall()
    for row in rows:
        tools = json.loads(row["tool_names_json"] if isinstance(row, sqlite3.Row) else row[5])
        if tool_name in tools:
            return row
    return None


def default_low_trust_caller(
    *,
    purpose: str = DEFAULT_AGENT_PURPOSE,
    device_id: str = DEFAULT_DEVICE_ID,
    transport: str = "stdio",
) -> Caller:
    """Represent an unregistered local caller explicitly; transport grants no trust."""
    return Caller(
        agent_id="agent:unregistered",
        display_name="Unregistered local agent",
        purpose=purpose,
        capability_id=None,
        device_id=device_id,
        transport=transport,
    )


def caller_from_token(
    conn: sqlite3.Connection,
    token: str | None,
    *,
    purpose: str = DEFAULT_AGENT_PURPOSE,
    tool_name: str = "resolve",
    device_id: str = DEFAULT_DEVICE_ID,
    transport: str = "stdio",
) -> Caller:
    """Authenticate a configured caller, or return the explicit low-trust default."""
    if token is None or not token.strip():
        return default_low_trust_caller(purpose=purpose, device_id=device_id, transport=transport)
    token_hash = hash_agent_token(token.strip())
    now = _utc_iso()
    row = conn.execute(
        """SELECT t.id AS token_id, t.agent_id, p.display_name, p.trust_state,
                  p.revoked_at AS principal_revoked_at
           FROM agent_tokens t JOIN agent_principals p ON p.id=t.agent_id
           WHERE t.token_hash=? AND t.revoked_at IS NULL AND t.valid_until>?""",
        (token_hash, now),
    ).fetchone()
    if row is None:
        raise AgentAuthenticationError("agent token is invalid, expired, or revoked")
    get = (
        (lambda key, index: row[key])
        if isinstance(row, sqlite3.Row)
        else (lambda key, index: row[index])
    )
    if get("trust_state", 3) != "active" or get("principal_revoked_at", 4) is not None:
        raise AgentAuthenticationError("agent principal is revoked")
    agent_id = str(get("agent_id", 1))
    capability = _capability_for(conn, agent_id, purpose=purpose, tool_name=tool_name)
    if capability is None:
        raise AgentAuthenticationError(
            f"agent has no active capability for {tool_name!r} and purpose {purpose!r}"
        )
    cap_get = (
        (lambda key, index: capability[key])
        if isinstance(capability, sqlite3.Row)
        else (lambda key, index: capability[index])
    )
    conn.execute(
        "UPDATE agent_tokens SET last_used_at=? WHERE id=?",
        (_utc_iso(), get("token_id", 0)),
    )
    conn.commit()
    return Caller(
        agent_id=agent_id,
        display_name=str(get("display_name", 2)),
        purpose=purpose,
        capability_id=str(cap_get("id", 0)),
        device_id=device_id,
        transport=transport,
    )


def capability_limits(conn: sqlite3.Connection, caller: Caller) -> tuple[int, int]:
    """Return bounded disclosure limits for a caller's active capability."""
    if caller.capability_id is None:
        return LOW_TRUST_MAX_ITEMS, LOW_TRUST_MAX_CHARS
    row = conn.execute(
        """SELECT max_items, max_chars FROM agent_capabilities
           WHERE id=? AND agent_id=? AND revoked_at IS NULL AND valid_until>?""",
        (caller.capability_id, caller.agent_id, _utc_iso()),
    ).fetchone()
    if row is None:
        raise AgentAuthenticationError("agent capability is expired or revoked")
    return int(row[0]), int(row[1])


def revoke_agent(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    token_id: str | None = None,
    capability_id: str | None = None,
) -> int:
    """Revoke a principal or one of its credentials/capabilities."""
    now = _utc_iso()
    if token_id is not None:
        cursor = conn.execute(
            "UPDATE agent_tokens SET revoked_at=? WHERE id=? AND agent_id=? AND revoked_at IS NULL",
            (now, token_id, agent_id),
        )
    elif capability_id is not None:
        cursor = conn.execute(
            """UPDATE agent_capabilities SET revoked_at=?
               WHERE id=? AND agent_id=? AND revoked_at IS NULL""",
            (now, capability_id, agent_id),
        )
    else:
        cursor = conn.execute(
            """UPDATE agent_principals SET trust_state='revoked', revoked_at=?
               WHERE id=? AND trust_state='active'""",
            (now, agent_id),
        )
        conn.execute(
            "UPDATE agent_tokens SET revoked_at=? WHERE agent_id=? AND revoked_at IS NULL",
            (now, agent_id),
        )
        conn.execute(
            "UPDATE agent_capabilities SET revoked_at=? WHERE agent_id=? AND revoked_at IS NULL",
            (now, agent_id),
        )
    conn.commit()
    return cursor.rowcount


def _text(value: object, *, style: str | None = None) -> Text:
    return Text(str(value), style=style)


def _short(value: object, limit: int = 78) -> str:
    clean = " ".join(str(value or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def count_noun(count: int, singular: str, plural: str | None = None) -> str:
    """A count and the word for it. The overview said "1 entries · 1 chunks"."""
    return f"{count:,} {singular if count == 1 else plural or singular + 's'}"


class TrustCenter:
    """A dependency-light guided interface over Varyz's existing controls."""

    def __init__(
        self,
        config_path: Path,
        conn: sqlite3.Connection,
        console: Console,
        *,
        prompt: Prompt | None = None,
        confirm: Confirm | None = None,
    ) -> None:
        self.config_path = config_path.resolve()
        self.cfg = load_config(self.config_path)
        self.conn = conn
        self.console = console
        self.prompt = prompt or (lambda label: typer.prompt(label))
        self.confirm = confirm or (lambda label: typer.confirm(label, default=False))

    def run(self) -> None:
        self._banner()
        sections = [
            ("Overview and health", self.overview),
            ("Sources and autosync", self.sources),
            ("Knowledge graph review", self.graph),
            ("Privacy, receipts, and consent", self.privacy),
            ("Delete or restore exclusions", self.deletion),
        ]
        if not builder_is_shipped():
            # The alpha builds no graph (docs/SHIPPING.md): nothing to review.
            sections = [section for section in sections if section[1] != self.graph]
        actions = {str(number): action for number, (_, action) in enumerate(sections, 1)}
        while True:
            menu = Table.grid(padding=(0, 2))
            for number, (label, _) in enumerate(sections, 1):
                menu.add_row(str(number), label)
            menu.add_row("0", "Exit")
            self.console.print(menu)
            try:
                choice = self.prompt("Choose a section").strip().lower()
            except typer.Abort:
                # A pipe, a scheduled task or a double-clicked window reaches the
                # menu and answers EOF, and click printed a bare "Aborted." The
                # overview this centre opens with needs no answer at all.
                self.console.print("No settings changed.")
                self.console.print(
                    "[dim]For the overview without prompts: varyz trust --summary[/dim]"
                )
                return
            if choice in {"0", "q", "quit", "exit"}:
                self.console.print("No settings changed unless confirmed above.")
                return
            action = actions.get(choice)
            if action is None:
                self.console.print(f"[yellow]Choose 0–{len(sections)}.[/yellow]")
                continue
            action()

    def _banner(self) -> None:
        self.console.print(
            Panel.fit(
                "[bold]Varyz trust center[/bold]\n"
                "Inspect first. Nothing leaves this machine or changes without confirmation.",
                border_style="cyan",
            )
        )

    def overview(self) -> None:
        active = self.conn.execute(
            """SELECT id, activated_at, entries_processed FROM graph_runs
               WHERE state='active'"""
        ).fetchone()
        state = load_state(self.cfg)
        summary = Table(title="Local trust overview", show_header=False, box=None)
        summary.add_column(style="dim", no_wrap=True)
        summary.add_column()
        summary.add_row(
            "Archive",
            f"{count_noun(dao.count_entries(self.conn), 'entry', 'entries')} · "
            f"{count_noun(_count(self.conn, 'SELECT COUNT(*) FROM chunks'), 'chunk')}",
        )
        summary.add_row(
            "Sources",
            f"{len(self.cfg.connectors)} configured · "
            f"{_count(self.conn, 'SELECT COUNT(DISTINCT source) FROM entries')} indexed",
        )
        # The alpha builds no graph (docs/SHIPPING.md), so it has no row.
        if active and builder_is_shipped():
            claims = _count(
                self.conn,
                "SELECT COUNT(*) FROM claims WHERE graph_generation_id=?",
                (active["id"],),
            )
            pending = _count(
                self.conn,
                """SELECT COUNT(*) FROM entity_candidates
                   WHERE graph_generation_id=? AND state='pending'""",
                (active["id"],),
            )
            summary.add_row(
                "Graph",
                f"active · {count_noun(claims, 'claim')} · {count_noun(pending, 'review')}",
            )
        elif builder_is_shipped():
            summary.add_row("Graph", "not built")
        autosync = "disabled"
        if is_enabled(self.cfg):
            autosync = "paused" if is_paused(self.cfg) else "active"
        if state:
            autosync += f" · last {state.get('status', 'unknown')} {state.get('finished_at', '?')}"
        summary.add_row("Autosync", autosync)
        grants = _count(self.conn, "SELECT COUNT(*) FROM consent_grants WHERE revoked_at IS NULL")
        summary.add_row(
            "Privacy",
            f"{count_noun(_count(self.conn, 'SELECT COUNT(*) FROM egress_receipts'), 'receipt')} · "
            f"{count_noun(grants, 'unrevoked grant')}",
        )
        summary.add_row(
            "Excluded",
            f"{count_noun(_count(self.conn, 'SELECT COUNT(*) FROM entry_exclusions'), 'item')} "
            "blocked from re-import",
        )
        self.console.print(summary)

        privacy_issues = diagnose_privacy(self.conn)
        graph_errors = _count(
            self.conn,
            """SELECT COUNT(*) FROM graph_runs WHERE state='failed'""",
        ) if builder_is_shipped() else 0
        if not privacy_issues and not graph_errors:
            self.console.print(
                "[green]No recorded privacy or graph-integrity failures.[/green]"
                if builder_is_shipped() else "[green]No recorded privacy failures.[/green]"
            )
        else:
            # "Needs attention: 1 privacy checks, 0 failed graph runs" named a
            # count and not one thing to go and look at -- and counted the
            # category that was fine beside the one that was not.
            self.console.print("[yellow]Needs attention:[/yellow]")
            for issue in privacy_issues:
                self.console.print(f"  · {issue.message} ({issue.count})")
            if graph_errors:
                self.console.print(f"  · {count_noun(graph_errors, 'failed graph run')}")
            if privacy_issues:
                self.console.print("[dim]  Run: varyz privacy doctor[/dim]")
            if graph_errors:
                self.console.print("[dim]  Run: varyz graph doctor[/dim]")

    def sources(self) -> None:
        while True:
            self.cfg = load_config(self.config_path)
            rows = self._source_rows()
            if self.console.width < 88:
                self._print_sources_narrow(rows)
            else:
                self._print_sources_table(rows)
            self.console.print(
                "[dim]Select a source to pause/resume or sync; A shows setup commands; "
                "P toggles global autosync; 0 returns.[/dim]"
            )
            choice = self.prompt("Source").strip().lower()
            if choice in {"0", "b", "back"}:
                return
            if choice == "a":
                self._source_setup_help()
                continue
            if choice == "p":
                self._toggle_autosync()
                continue
            try:
                row = rows[int(choice) - 1]
            except (ValueError, IndexError):
                self.console.print("[yellow]Choose a listed source, A, P, or 0.[/yellow]")
                continue
            self._source_action(str(row["id"]), bool(row["enabled"]))

    def _print_sources_table(self, rows: list[dict[str, object]]) -> None:
        table = Table(title="Sources", show_lines=False)
        table.add_column("#", style="dim", justify="right")
        table.add_column("source")
        table.add_column("state")
        table.add_column("indexed", justify="right")
        table.add_column("last sync")
        table.add_column("scope / account")
        for index, row in enumerate(rows, 1):
            table.add_row(
                str(index),
                _text(row["id"]),
                str(row["state"]),
                str(row["count"]),
                _text(row["last"]),
                _text(row["scope"]),
            )
        self.console.print(table)

    def _print_sources_narrow(self, rows: list[dict[str, object]]) -> None:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=3, justify="right", style="dim")
        table.add_column(width=9, style="dim")
        table.add_column(ratio=1)
        table.add_row("", "", Text("Sources", style="bold italic"))
        for index, row in enumerate(rows, 1):
            table.add_row(
                str(index),
                "source",
                _text(
                    f"{row['id']} · {row['state']} · {row['count']} indexed",
                    style="bold",
                ),
            )
            table.add_row("", "last", _text(row["last"]))
            table.add_row("", "scope", _text(row["scope"]))
            if index != len(rows):
                table.add_row("", "", "")
        self.console.print(table)

    def _source_rows(self) -> list[dict[str, object]]:
        sync_rows = {
            row["connector_id"]: row
            for row in self.conn.execute(
                """SELECT connector_id, MAX(last_run_at) AS last_run_at,
                          MAX(last_status) AS last_status
                   FROM sync_state GROUP BY connector_id"""
            )
        }
        counts = {
            row["source"]: row["count"]
            for row in self.conn.execute(
                "SELECT source, COUNT(*) AS count FROM entries GROUP BY source"
            )
        }
        result: list[dict[str, object]] = []
        for connector_id in sorted(self.cfg.connectors):
            options = self.cfg.connector_options(connector_id)
            enabled = options.get("enabled", True) is not False
            approved = options.get("approved", True) is not False
            if enabled and approved:
                state = "active"
            elif not enabled:
                state = "paused"
            else:
                state = "not approved"
            synced = sync_rows.get(connector_id)
            last = "never"
            if synced:
                last = f"{synced['last_run_at'] or '?'} · {synced['last_status'] or '?'}"
            result.append(
                {
                    "id": connector_id,
                    "enabled": enabled,
                    "state": state,
                    "count": int(counts.get(connector_id, 0)),
                    "last": last,
                    "scope": self._safe_source_scope(connector_id, options),
                }
            )
        return result

    @staticmethod
    def _safe_source_scope(connector_id: str, options: dict) -> str:
        if connector_id == "calendar":
            return f"{options.get('account', 'default')} · secret URL in credential manager"
        if connector_id in {"gmail", "github"}:
            return str(options.get("account") or options.get("login") or "configured account")
        paths = options.get("paths") or []
        if paths:
            return f"{len(paths)} approved path(s)"
        if options.get("db_path"):
            return "approved local store"
        return str(options.get("account") or "configured")

    def _source_action(self, connector_id: str, enabled: bool) -> None:
        action = self.prompt("Action: 1 pause/resume, 2 sync now, 0 cancel").strip()
        if action == "1":
            new_state = not enabled
            verb = "resume" if new_state else "pause"
            if not self.confirm(f"{verb.title()} {connector_id} ingestion?"):
                return
            upsert_connector(self.config_path, connector_id, {"enabled": new_state})
            self.console.print(f"[green]{connector_id} {verb}d.[/green]")
        elif action == "2":
            if not enabled:
                self.console.print("[yellow]Resume this source before syncing it.[/yellow]")
                return
            if not self.confirm(f"Sync {connector_id} now?"):
                return
            cfg = load_config(self.config_path)
            try:
                with WriteLock(cfg.data_dir):
                    stats = sync_connector(
                        self.conn,
                        Vault(cfg.vault_dir),
                        connector_registry.get_connector(connector_id),
                        cfg,
                    )
                    embedded = embed_pending(self.conn, cfg, Embedder(cfg))
            except Exception as exc:
                # _safe_error swaps every URL for "[redacted-url]", which Rich
                # reads as a style tag: unescaped, the marker vanished and the
                # message read as though it had been cut off mid-sentence.
                self.console.print(f"[red]Sync failed: {escape(_safe_error(exc))}[/red]")
                return
            self.console.print(
                f"[green]{connector_id} synced:[/green] {stats.new} new, "
                f"{stats.updated} updated, {stats.excluded} excluded, "
                f"{stats.errors} errors · {embedded} chunks embedded."
            )

    def _source_setup_help(self) -> None:
        commands = Table(title="Add or reconnect a source")
        commands.add_column("source")
        commands.add_column("guided command")
        # The alpha's sources only (docs/SHIPPING.md), from the one list
        # `varyz sources` and `varyz setup` read too: this table offered
        # Gmail, WhatsApp and a `varyz bootstrap` that no longer existed.
        from varyz.sources import ALPHA_SOURCES

        for info in ALPHA_SOURCES:
            command = info.connect
            if command.startswith("varyz ") and '"' not in command:
                command = f"{command} --config {self.config_path}"
            commands.add_row(info.label, command)
        self.console.print(commands)
        self.console.print(
            "[dim]Account sign-in remains in the dedicated masked connector wizard; "
            "the trust center never echoes secrets.[/dim]"
        )

    def _toggle_autosync(self) -> None:
        if not is_enabled(self.cfg):
            self.console.print(
                "Autosync is not installed. Run: "
                f"varyz autosync install --config {self.config_path}"
            )
            return
        if is_paused(self.cfg):
            if self.confirm("Resume all automatic source refreshes?"):
                resume(self.cfg)
                self.console.print("[green]Autosync resumed.[/green]")
        elif self.confirm("Pause all automatic source refreshes?"):
            pause(self.cfg)
            self.console.print("[yellow]Autosync paused.[/yellow]")

    def graph(self) -> None:
        active = self.conn.execute(
            "SELECT id, activated_at FROM graph_runs WHERE state='active'"
        ).fetchone()
        if active is None:
            self.console.print(
                "No active graph. Run [bold]varyz graph rebuild[/bold] before reviewing it."
            )
            return
        while True:
            claims = _count(
                self.conn,
                "SELECT COUNT(*) FROM claims WHERE graph_generation_id=?",
                (active["id"],),
            )
            pending = _count(
                self.conn,
                """SELECT COUNT(*) FROM entity_candidates
                   WHERE graph_generation_id=? AND state='pending'""",
                (active["id"],),
            )
            self.console.print(
                Panel.fit(
                    f"Active graph · {claims:,} claims · {pending:,} identity reviews\n"
                    "1 Review identities   2 Find/correct claims   0 Back",
                    title="Knowledge graph",
                )
            )
            choice = self.prompt("Graph action").strip()
            if choice == "0":
                return
            if choice == "1":
                self._review_identities(active["id"])
            elif choice == "2":
                self._review_claims(active["id"])
            else:
                self.console.print("[yellow]Choose 0, 1, or 2.[/yellow]")

    def _review_identities(self, run_id: str) -> None:
        rows = self.conn.execute(
            """SELECT c.id, c.fingerprint, l.canonical_label AS left_label,
                      r.canonical_label AS right_label, c.score, c.explanation
               FROM entity_candidates c
               JOIN entities l ON l.id=c.left_entity_id
               JOIN entities r ON r.id=c.right_entity_id
               WHERE c.graph_generation_id=? AND c.state='pending'
               ORDER BY c.score DESC, c.id LIMIT 50""",
            (run_id,),
        ).fetchall()
        if not rows:
            self.console.print("[green]No identity candidates need review.[/green]")
            return
        table = Table(title="Could these be the same identity?")
        table.add_column("#", justify="right", style="dim")
        table.add_column("left")
        table.add_column("right")
        table.add_column("score", justify="right")
        table.add_column("why")
        for index, row in enumerate(rows, 1):
            table.add_row(
                str(index),
                _text(row["left_label"]),
                _text(row["right_label"]),
                f"{row['score']:.2f}",
                _text(_short(row["explanation"], 60)),
            )
        self.console.print(table)
        choice = self.prompt("Candidate number, or 0 to cancel").strip()
        if choice == "0":
            return
        try:
            selected = rows[int(choice) - 1]
        except (ValueError, IndexError):
            self.console.print("[yellow]No such candidate.[/yellow]")
            return
        action = self.prompt("Decision: 1 merge, 2 keep separate, 0 later").strip()
        action_name = {"1": "merge", "2": "keep_separate"}.get(action)
        if action_name is None:
            return
        if not self.confirm(
            f"Record '{action_name.replace('_', ' ')}' for "
            f"{selected['left_label']} / {selected['right_label']}?"
        ):
            return
        with WriteLock(self.cfg.data_dir):
            override_id = append_override(
                self.conn,
                "candidate",
                selected["fingerprint"],
                action_name,
                reason="trust_center_review",
            )
        self.console.print(f"[green]Decision saved.[/green] Override {override_id}")

    def _review_claims(self, run_id: str) -> None:
        query = self.prompt("Find claims about").strip()
        if not query:
            return
        pattern = f"%{query}%"
        rows = self.conn.execute(
            """SELECT c.fingerprint, e.canonical_label, c.predicate,
                      c.object_surface, c.valid_from, c.valid_to,
                      c.modality, c.lifecycle_state, c.sensitivity
               FROM claims c JOIN entities e ON e.id=c.subject_entity_id
               WHERE c.graph_generation_id=? AND
                     (e.canonical_label LIKE ? OR c.predicate LIKE ? OR
                      c.object_surface LIKE ?)
               ORDER BY COALESCE(c.valid_from, c.asserted_at, c.observed_at) DESC
               LIMIT 30""",
            (run_id, pattern, pattern, pattern),
        ).fetchall()
        if not rows:
            self.console.print("No matching claims.")
            return
        table = Table(title="Claims")
        table.add_column("#", justify="right", style="dim")
        table.add_column("subject")
        table.add_column("claim")
        table.add_column("time")
        table.add_column("state")
        for index, row in enumerate(rows, 1):
            interval = row["valid_from"] or "unknown"
            if row["valid_to"]:
                interval += f" → {row['valid_to']}"
            table.add_row(
                str(index),
                _text(row["canonical_label"]),
                _text(f"{row['predicate']} · {_short(row['object_surface'], 55)}"),
                _text(interval),
                f"{row['lifecycle_state']} · {row['sensitivity']}",
            )
        self.console.print(table)
        choice = self.prompt("Claim number, or 0 to cancel").strip()
        if choice == "0":
            return
        try:
            selected = rows[int(choice) - 1]
        except (ValueError, IndexError):
            self.console.print("[yellow]No such claim.[/yellow]")
            return
        evidence = self.conn.execute(
            """SELECT x.quote, x.evidence_role, e.source, e.title, e.external_id
               FROM claims c JOIN claim_evidence x ON x.claim_id=c.id
               JOIN entries e ON e.id=x.entry_id
               WHERE c.graph_generation_id=? AND c.fingerprint=?""",
            (run_id, selected["fingerprint"]),
        ).fetchall()
        for item in evidence:
            self.console.print(
                Panel(
                    _text(item["quote"]),
                    title=_text(
                        f"{item['evidence_role']} · {item['source']} · "
                        f"{_short(item['title'] or item['external_id'], 55)}"
                    ),
                )
            )
        action = self.prompt(
            "Correction: 1 accept, 2 reject, 3 time, 4 sensitivity, 0 cancel"
        ).strip()
        if action == "0":
            return
        name = {"1": "accept", "2": "reject"}.get(action)
        payload: dict = {}
        if action == "3":
            name = "correct_time"
            payload = {
                "valid_from": self.prompt("Correct start (ISO/date; blank unknown)").strip()
                or None,
                "valid_to": self.prompt("Correct end (ISO/date; blank open)").strip() or None,
                "temporal_precision": self.prompt(
                    "Precision: exact/day/month/year/decade/unknown"
                ).strip()
                or "unknown",
            }
        elif action == "4":
            name = "change_sensitivity"
            sensitivity = self.prompt("Sensitivity: public/personal/sensitive/unclassified").strip()
            try:
                Sensitivity(sensitivity)
            except ValueError:
                self.console.print("[yellow]Invalid sensitivity.[/yellow]")
                return
            payload = {"sensitivity": sensitivity}
        if name is None:
            self.console.print("[yellow]Unknown correction.[/yellow]")
            return
        if not self.confirm(f"Persist this {name.replace('_', ' ')} correction?"):
            return
        try:
            with WriteLock(self.cfg.data_dir):
                override_id = append_override(
                    self.conn,
                    "claim",
                    selected["fingerprint"],
                    name,
                    payload,
                    reason="trust_center_review",
                )
        except ValueError as exc:
            self.console.print(f"[red]{escape(str(exc))}[/red]")
            return
        self.console.print(f"[green]Correction saved.[/green] Override {override_id}")

    def privacy(self) -> None:
        while True:
            self.console.print("1 Recent receipts   2 Consent grants   3 Privacy health   0 Back")
            choice = self.prompt("Privacy action").strip()
            if choice == "0":
                return
            if choice == "1":
                self._receipts()
            elif choice == "2":
                self._grants()
            elif choice == "3":
                self._privacy_health()
            else:
                self.console.print("[yellow]Choose 0–3.[/yellow]")

    def _receipts(self) -> None:
        rows = self.conn.execute(
            """SELECT r.id, r.created_at, r.payload_hash, r.payload_item_count,
                      r.payload_char_count, r.decision_summary_json,
                      q.purpose, q.destination,
                      (SELECT state FROM egress_executions x
                       WHERE x.receipt_id=r.id ORDER BY attempt DESC LIMIT 1) AS state
               FROM egress_receipts r JOIN context_requests q ON q.id=r.request_id
               ORDER BY r.created_at DESC LIMIT 30"""
        ).fetchall()
        if not rows:
            self.console.print("No egress receipts yet.")
            return
        table = Table(title="Context leaving this machine")
        table.add_column("#", justify="right", style="dim")
        table.add_column("time")
        table.add_column("destination")
        table.add_column("purpose")
        table.add_column("payload")
        table.add_column("state")
        for index, row in enumerate(rows, 1):
            table.add_row(
                str(index),
                local_stamp(row["created_at"]),
                _text(row["destination"]),
                _text(row["purpose"]),
                f"{row['payload_item_count']} items · {row['payload_char_count']} chars",
                row["state"] or "prepared",
            )
        self.console.print(table)
        choice = self.prompt("Receipt number to inspect, or 0").strip()
        if choice == "0":
            return
        try:
            receipt = rows[int(choice) - 1]
        except (ValueError, IndexError):
            self.console.print("[yellow]No such receipt.[/yellow]")
            return
        decisions = self.conn.execute(
            """SELECT d.resource_type, d.resource_id, d.effect,
                      d.transformation, d.reason_codes_json
               FROM context_decisions d JOIN egress_receipts r
                 ON r.request_id=d.request_id WHERE r.id=?
               ORDER BY d.effect, d.resource_type, d.resource_id""",
            (receipt["id"],),
        ).fetchall()
        detail = Table(title=f"Receipt {receipt['id']}")
        detail.add_column("effect")
        detail.add_column("item")
        detail.add_column("source label")
        detail.add_column("reason")
        for decision in decisions:
            label = decision["resource_id"]
            if decision["resource_type"] == "evidence_excerpt":
                entry = dao.get_entry(self.conn, decision["resource_id"])
                if entry:
                    label = entry.title or _short(entry.body, 50)
            reasons = ", ".join(json.loads(decision["reason_codes_json"] or "[]"))
            if decision["transformation"]:
                reasons += f" · transformed:{decision['transformation']}"
            detail.add_row(
                decision["effect"],
                f"{decision['resource_type']}:{decision['resource_id']}",
                _text(_short(label, 55)),
                _text(reasons),
            )
        self.console.print(detail)
        self.console.print(f"payload SHA-256: {receipt['payload_hash']}")
        self.console.print(
            "[dim]Receipts intentionally store the hash and source decisions, not a second "
            "copy of private payload text. Interactive cloud calls show the exact payload "
            "before approval.[/dim]"
        )

    def _grants(self) -> None:
        grants = ReceiptStore(self.conn).consent_grants()
        if not grants:
            self.console.print("No consent grants.")
            return
        table = Table(title="Consent grants")
        table.add_column("#", justify="right", style="dim")
        table.add_column("destination")
        table.add_column("purpose")
        table.add_column("budget")
        table.add_column("valid until")
        table.add_column("state")
        for index, grant in enumerate(grants, 1):
            item_limit = "∞" if grant.max_items is None else str(grant.max_items)
            char_limit = "∞" if grant.max_chars is None else str(grant.max_chars)
            budget = (
                f"{grant.used_items}/{item_limit} items · {grant.used_chars}/{char_limit} chars"
            )
            state = "revoked" if grant.revoked_at else "available"
            table.add_row(
                str(index),
                _text(grant.destination),
                _text(grant.purpose),
                budget,
                str(grant.valid_until or "no expiry"),
                state,
            )
        self.console.print(table)
        choice = self.prompt("Grant number to revoke, or 0").strip()
        if choice == "0":
            return
        try:
            grant = grants[int(choice) - 1]
        except (ValueError, IndexError):
            self.console.print("[yellow]No such grant.[/yellow]")
            return
        if grant.revoked_at:
            self.console.print("That grant is already revoked.")
            return
        if self.confirm(f"Revoke future use of this {grant.destination} grant?"):
            with WriteLock(self.cfg.data_dir):
                ReceiptStore(self.conn).revoke_grant(grant.id)
            self.console.print("[green]Consent revoked for future requests.[/green]")

    def _privacy_health(self) -> None:
        issues = diagnose_privacy(self.conn)
        if not issues:
            self.console.print("[green]Privacy boundary is healthy.[/green]")
            return
        table = Table(title="Privacy health")
        table.add_column("severity")
        table.add_column("check")
        table.add_column("count", justify="right")
        table.add_column("detail")
        for issue in issues:
            table.add_row(issue.severity, issue.code, str(issue.count), _text(issue.message))
        self.console.print(table)

    def deletion(self) -> None:
        while True:
            exclusions = dao.list_entry_exclusions(self.conn)
            self.console.print(
                "1 Find and delete an indexed item   "
                f"2 Review {len(exclusions)} exclusions   0 Back"
            )
            choice = self.prompt("Deletion action").strip()
            if choice == "0":
                return
            if choice == "1":
                self._delete_entry()
            elif choice == "2":
                self._review_exclusions(exclusions)
            else:
                self.console.print("[yellow]Choose 0–2.[/yellow]")

    def _delete_entry(self) -> None:
        query = self.prompt("Find by title, text, source, or ID").strip()
        if not query:
            return
        pattern = f"%{query}%"
        rows = self.conn.execute(
            """SELECT id, source, source_account, external_id, title, body,
                      occurred_start, sensitivity, vault_ref
               FROM entries WHERE id LIKE ? OR title LIKE ? OR body LIKE ?
                    OR source LIKE ? OR external_id LIKE ?
               ORDER BY COALESCE(occurred_start, ingested_at) DESC LIMIT 30""",
            (pattern, pattern, pattern, pattern, pattern),
        ).fetchall()
        if not rows:
            self.console.print("No matching indexed items.")
            return
        table = Table(title="Local indexed items")
        table.add_column("#", justify="right", style="dim")
        table.add_column("date")
        table.add_column("source")
        table.add_column("item")
        table.add_column("sensitivity")
        for index, row in enumerate(rows, 1):
            table.add_row(
                str(index),
                local_day(row["occurred_start"]),
                _text(row["source"]),
                _text(_short(row["title"] or row["body"], 70)),
                row["sensitivity"],
            )
        self.console.print(table)
        choice = self.prompt("Item number, or 0 to cancel").strip()
        if choice == "0":
            return
        try:
            selected = rows[int(choice) - 1]
        except (ValueError, IndexError):
            self.console.print("[yellow]No such item.[/yellow]")
            return
        chunks = _count(
            self.conn,
            "SELECT COUNT(*) FROM chunks WHERE entry_id=?",
            (selected["id"],),
        )
        evidence = _count(
            self.conn,
            "SELECT COUNT(*) FROM claim_evidence WHERE entry_id=?",
            (selected["id"],),
        )
        todos = _count(self.conn, "SELECT COUNT(*) FROM todos WHERE entry_id=?", (selected["id"],))
        self.console.print(
            Panel(
                f"Varyz will remove this local item, {chunks} search chunks, "
                f"{evidence} graph evidence links, and {todos} linked tasks.\n"
                "The upstream file/message/event will not be touched.",
                title=_text(_short(selected["title"] or selected["external_id"], 65)),
                border_style="red",
            )
        )
        action = self.prompt(
            "1 Delete and block re-import (recommended), 2 delete once, 0 cancel"
        ).strip()
        if action not in {"1", "2"}:
            return
        durable = action == "1"
        if not self.confirm("Permanently remove this item from the local Varyz archive?"):
            return
        with WriteLock(self.cfg.data_dir):
            forget_entry(
                self.conn,
                Vault(self.cfg.vault_dir),
                selected["id"],
                prevent_reimport=durable,
            )
        suffix = " It is blocked from re-import." if durable else " A later sync may restore it."
        self.console.print(f"[green]Deleted from Varyz.[/green]{suffix}")

    def _review_exclusions(self, exclusions: list[sqlite3.Row]) -> None:
        if not exclusions:
            self.console.print("No items are blocked from re-import.")
            return
        table = Table(title="Blocked from re-import")
        table.add_column("#", justify="right", style="dim")
        table.add_column("source")
        table.add_column("account")
        table.add_column("item key")
        table.add_column("blocked at")
        for index, row in enumerate(exclusions, 1):
            table.add_row(
                str(index),
                _text(row["source"]),
                _text(row["source_account"] or "default"),
                _text(_short(row["external_id"], 62)),
                local_stamp(row["created_at"]),
            )
        self.console.print(table)
        choice = self.prompt("Exclusion number to allow again, or 0").strip()
        if choice == "0":
            return
        try:
            selected = exclusions[int(choice) - 1]
        except (ValueError, IndexError):
            self.console.print("[yellow]No such exclusion.[/yellow]")
            return
        if not self.confirm("Allow this item to be imported again on a future sync?"):
            return
        with WriteLock(self.cfg.data_dir), self.conn:
            dao.remove_entry_exclusion(
                self.conn,
                selected["source"],
                selected["source_account"],
                selected["external_id"],
            )
        self.console.print("[green]Exclusion removed.[/green] A future sync may restore the item.")


def run_trust_center(
    config_path: Path,
    conn: sqlite3.Connection,
    console: Console,
    *,
    summary: bool = False,
) -> None:
    center = TrustCenter(config_path, conn, console)
    if summary:
        center._banner()
        center.overview()
        return
    center.run()
