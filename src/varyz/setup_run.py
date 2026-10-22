"""`varyz setup`'s seven steps, run once for the terminal and the page alike.

The steps are archive, daemon, agents, data sources, graph, autosync and
classifier. They used to be written twice, once for each place setup asks,
and the two copies drifted: the terminal numbered its steps wrongly, missed
an agent whose connect raised, and printed GO over a source that failed to
index. Now `SetupRun` runs them and decides GO or NO-GO. The terminal
(`TerminalSetup`) and the page (`setup_web.SetupSession`) only show what it
does, each through `_set` (a step's state) and `_bar` (a long step's
progress). What each asks before the run differs, and stays theirs:
y/n questions in the terminal, ticks on the page.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varyz.bootstrap import DetectedSources

STEPS = ("Archive", "Daemon", "Agents", "Data sources", "Graph", "Autosync", "Classifier")
# Said as step 4 reads and indexes on this machine, and on the page as it does.
WARM_NOTE = "This can make your computer warm and use battery faster. That's normal."

OnProgress = Callable[[str, float | None], None]


@dataclass
class Step:
    name: str
    # waiting, running, paused, done, failed or skipped; absent where this
    # build has no such step (the alpha's graph), which no view shows.
    state: str = "waiting"
    detail: str = ""


@dataclass
class _Run:
    """What the first steps decided, kept for the rest: a paused run resumes here."""

    cfg: Any
    agents_connected: bool = False
    agents_changed: bool = False
    # The agents step 3 connected, by name: the first to use varyz starts the reading.
    agent_names: list[str] = field(default_factory=list)
    problems: list[tuple[str, str]] = field(default_factory=list)
    # Step 4's sources: those newly connected (named in its line) and those indexed.
    picked: list[str] = field(default_factory=list)
    to_sync: tuple[str, ...] = ()
    failed: list[str] = field(default_factory=list)
    installed_at: float = 0.0
    moved: bool = False


class SetupRun:
    """The steps, in order, and the GO / NO-GO they end with.

    What is offered is decided here, before anything changes: the agents
    found on this machine and the data sources not yet in the config.
    ``execute`` connects only those the owner chose, from what was offered.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        detected: DetectedSources | None = None,
        wanted: tuple[str, ...] | None = None,
    ) -> None:
        from varyz import cli
        from varyz.bootstrap import discover_local_sources, missing_detected_sources
        from varyz.install import client_paths

        self.config_path = config_path
        from varyz.setup_machine import plan

        self.ai_plan = plan(config_path)
        self.inference_mode = self.ai_plan["selected"]
        self.cloud_conversations = self.ai_plan["cloud_conversations"]
        self.analytics_choice: bool | None = None
        self._analytics_cfg: Any = None
        self._lock = threading.Lock()
        self.steps = [Step(name) for name in STEPS]
        from varyz.graph import builder_is_shipped

        if not builder_is_shipped():
            # The alpha holds the graph builder back: that step never runs.
            self.steps[STEPS.index("Graph")].state = "absent"
        self.result: dict[str, Any] | None = None
        self.detected = missing_detected_sources(
            config_path, detected if detected is not None else discover_local_sources()
        )
        self.offers = cli._offered_sources(self.detected, wanted or cli.SETUP_SOURCES)
        paths = client_paths()
        self.agents = [
            (cid, name, cid in paths and paths[cid].exists()) for cid, name in cli.SETUP_AGENTS
        ]
        # GitHub, which only the page links before a run: the account already
        # in the config, else (login, token) until a run stores it.
        self.github_account: str | None = None
        self._github: tuple[str, str] | None = None
        # Why the data step is skipped, when the owner said so before the run.
        self.skip_sources: str | None = None
        self._resume_from: _Run | None = None

    @property
    def github_login(self) -> str | None:
        """The GitHub account a run would index: linked before, or on the page."""
        with self._lock:
            if self.github_account is not None:
                return self.github_account
            return self._github[0] if self._github is not None else None

    # -- what a view shows --------------------------------------------------------

    def _set(self, index: int, state: str, detail: str = "") -> None:
        with self._lock:
            self.steps[index].state = state
            self.steps[index].detail = detail
        self._analytics_step(index, state)

    def _analytics_step(self, index: int, state: str) -> None:
        if self._analytics_cfg is not None and state in {"done", "failed", "skipped"}:
            from varyz.analytics import record

            record(self._analytics_cfg, f"setup_{index}_{state}")

    def _progress(self, line: str, fraction: float | None = None) -> None:
        """What a long step is doing; a view that draws its own bar ignores it."""

    def _working_hard(self) -> None:
        """Step 4 is about to read and index on this machine; a view may say that is fine."""

    def _sources_read(self, run: _Run) -> None:
        """The chosen sources are in the archive; a view may offer to read the chats now.

        Called while the search models download, and again after step 4.
        """

    @contextlib.contextmanager
    def _bar(self, index: int | None, description: str) -> Iterator[OnProgress]:
        """Progress for a long stretch of step ``index`` (None: after the steps)."""
        self._progress(description)
        yield self._progress

    def _config_flag(self) -> str:
        from varyz import cli

        return cli._config_flag(self.config_path)

    # -- the run ------------------------------------------------------------------

    def execute(self, agents: tuple[str, ...], sources: tuple[str, ...]) -> dict[str, Any]:
        """Every step, in order; the result it ends with (also ``self.result``)."""
        from varyz import cli
        from varyz.bootstrap import approve_detected_sources, approved_local_connectors
        from varyz.install import connect_agent
        from varyz.resolution.client import ensure_daemon

        cfg_path = self.config_path

        # 1. The archive.
        self._set(0, "running")
        cfg, created, entries = cli._create_archive(cfg_path)
        previous_workflows = cfg.workflows
        from varyz.configfile import set_inference_mode

        set_inference_mode(cfg_path, self.inference_mode,
                           cloud_conversations=self.cloud_conversations)
        cfg = cli._cfg(cfg_path)
        mode_changed = previous_workflows != cfg.workflows
        from varyz.analytics import consent, record

        if self.analytics_choice is not None:
            consent(cfg, self.analytics_choice)
        self._analytics_cfg = cfg
        record(cfg, "setup_started")
        self._set(0, "done", f"{'created' if created else 'ready'}, {entries:,} entries")

        # 2. The daemon, which serves this archive's rules and owner questions.
        self._set(1, "running")
        # An already running daemon must stop using the previous reader settings.
        if not created and mode_changed and not cli._restart_daemon(cfg_path):
            self._set(1, "failed", "could not restart with the selected AI mode")
            return self._finish(cfg, problems=[("daemon", "AI mode restart failed")],
                                failed=[], agents_connected=False, agents_changed=False)
        status = ensure_daemon(cli._resolution_client(cfg), cfg_path)
        if status is None:
            self._set(1, "failed", "did not start; run `varyz daemon start --foreground`")
            for index in range(2, len(STEPS)):
                if self.steps[index].state != "absent":
                    self._set(index, "skipped", "not run: the daemon is down")
            return self._finish(cfg, problems=[("daemon", "did not start")], failed=[],
                                agents_connected=False, agents_changed=False)
        self._set(1, "done", f"running on {status.host}:{status.port}")

        # 3. The agents that will be briefed.
        self._set(2, "running")
        present = [(cid, name) for cid, name, installed in self.agents if installed]
        absent = [name for _, name, installed in self.agents if not installed]
        chosen = [(cid, name) for cid, name in present if cid in agents]
        agents_changed = agents_connected = failed_agents = False
        agent_failures: list[tuple[str, str]] = []
        agent_names: list[str] = []
        if not present:
            self._set(2, "skipped", f"none found ({', '.join(absent)} not installed)")
        elif not chosen:
            self._set(2, "skipped", f"none chosen; later: varyz connect {present[0][0]}")
        else:
            outcome = []
            for cid, name in chosen:
                try:
                    _, results = connect_agent(cid, config_path=cfg_path)
                except (ValueError, OSError, RuntimeError) as exc:
                    outcome.append(f"{name} failed ({exc})")
                    agent_failures.append((name, str(exc)))
                    failed_agents = True
                    continue
                skipped = [r.detail for r in results
                           if r.detail.startswith(("skipped", "not installed"))]
                if not results:
                    skipped = ["No connection settings were written"]
                if skipped:
                    # The skill and hooks may be in place, but without the
                    # MCP server the agent has nothing to call: not connected.
                    outcome.append(f"{name} failed ({skipped[0]})")
                    reason = skipped[0].removeprefix("skipped - ").removeprefix("skipped: ")
                    agent_failures.append((name, reason))
                    failed_agents = True
                    continue
                changed = any(result.changed for result in results)
                agents_changed = agents_changed or changed
                agents_connected = True
                agent_names.append(name)
                outcome.append(f"{name} {'connected' if changed else 'already connected'}")
            if absent:
                outcome.append(f"{', '.join(absent)} not installed")
            self._set(2, "failed" if failed_agents else "done", " · ".join(outcome))

        # 4. Data sources: only those offered that the owner chose.
        run = _Run(cfg, agents_connected=agents_connected,
                   agents_changed=agents_changed or mode_changed,
                   agent_names=agent_names)
        run.problems.extend(agent_failures)
        if self.skip_sources is not None:
            self._set(3, "skipped", self.skip_sources)
            return self._run_from(4, run)
        self._set(3, "running")
        run.picked = [cid for cid, _, _ in self.offers if cid in sources]
        if run.picked:
            approve_detected_sources(
                cfg_path,
                DetectedSources(
                    document_paths=(
                        self.detected.document_paths if "documents" in run.picked else ()
                    ),
                    git_paths=self.detected.git_paths if "git" in run.picked else (),
                    ai_adapters=(
                        self.detected.ai_adapters if "ai_conversations" in run.picked else ()
                    ),
                ),
            )
        github = "github" in sources and self.github_login is not None
        if github and self._github is not None:
            login, token = self._github
            cli._link_github(cfg_path, login, token)
            with self._lock:
                self.github_account, self._github = login, None
            run.picked.append("github")
        run.cfg = cli._cfg(cfg_path)
        run.to_sync = tuple(
            cid for cid in approved_local_connectors(cfg_path) if cid in cli.SETUP_SOURCES
        ) + (("github",) if github else ())
        return self._run_from(3, run)

    def _run_from(self, index: int, run: _Run) -> dict[str, Any]:
        """Step ``index`` (Data sources or later, 0-based) and every step after it.

        Where a paused run resumes: the step it paused in is run again, and
        its download carries on from what has arrived.
        """
        from varyz import cli
        from varyz.graph import builder_is_shipped

        cfg_path = self.config_path
        with self._lock:
            self._resume_from = run

        if index <= 3:
            # 4. Read the sources, then download the search models and index
            # them. The terminal's bar is drawn by `_sync_sources` itself.
            self._set(3, "running")
            if run.to_sync:
                self._working_hard()
            run.failed = (
                cli._sync_sources(run.cfg, run.to_sync, on_progress=self._progress, step=4,
                                  on_read=lambda: self._sources_read(run))
                if run.to_sync else []
            )
            labels = {cid: name for cid, name, _ in self.offers} | {"github": "GitHub"}
            if run.failed:
                self._set(3, "failed", f"could not index {', '.join(run.failed)}")
            elif run.to_sync:
                names = ", ".join(labels.get(cid, cid) for cid in run.picked)
                self._set(3, "done", f"connected {names}; indexed" if run.picked else "indexed")
            else:
                self._set(3, "skipped", "none connected")
        # Also with no source to read: the archive may hold chats from before.
        self._sources_read(run)

        if index <= 4 and builder_is_shipped():
            # 5. The graph, only where the builder ships (not in the alpha).
            self._set(4, "running")
            with self._bar(4, "Building the graph") as progress:
                state, detail = cli._build_graph(cfg_path, on_progress=progress)
            self._set(4, state, detail)
            if state == "failed":
                run.problems.append(("graph", f"{detail} (step 5)"))

        if index <= 5:
            # 6. Autosync, which keeps all of it current from now on.
            self._set(5, "running")
            run.installed_at = time.time()
            state, detail = cli._install_autosync(cfg_path)
            self._set(5, state, detail)
            if state == "failed":
                run.problems.append(("autosync", f"{detail} (step {cli._shown_step(6)[0]})"))

        # 7. The chosen reader, without downloading Ollama in conversation mode.
        self._set(6, "running")
        if self.inference_mode == "conversations":
            state = "done"
            detail = (
                "AI conversation mode; no Ollama or Qwen download. "
                + ("Eligible AI conversation excerpts only may be read by your connected AI; "
                   "local documents and repositories stay outside this reader."
                   if self.cloud_conversations else
                   "Cloud reading is off until you allow it; local fallbacks remain available.")
            )
        else:
            with self._bar(6, "Readying the classifier") as progress:
                state, detail, run.moved = cli._ready_classifier(cfg_path, on_progress=progress)
                if run.moved:
                    # The daemon read the old address when it started.
                    progress("Restarting the daemon on the new classifier address", None)
                    if not cli._restart_daemon(cfg_path):
                        state, detail = "failed", f"{detail}; the daemon did not start again"
        self._set(6, state, detail)
        if state == "failed":
            run.problems.append(("classifier", f"{detail} (step {cli._shown_step(7)[0]})"))

        privacy = None
        if self.steps[5].state == "done":
            with self._bar(None, "Checking what autosync can read"):
                privacy = cli._mac_folder_access(cfg_path, since=run.installed_at)
        return self._finish(cli._cfg(cfg_path), problems=run.problems, failed=run.failed,
                            agents_connected=run.agents_connected,
                            agents_changed=run.agents_changed or run.moved, privacy=privacy,
                            agent_names=run.agent_names)

    def _finish(
        self,
        cfg: Any,
        *,
        problems: list[tuple[str, str]],
        failed: list[str],
        agents_connected: bool,
        agents_changed: bool,
        privacy: dict[str, Any] | None = None,
        agent_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """What the owner has now, and what is still missing: GO or NO-GO."""
        from varyz import cli
        from varyz.db.connection import open_db
        from varyz.hooks import doctor
        from varyz.sources import source_statuses

        conn = open_db(cfg.db_path) if cfg.db_path.exists() else None
        from varyz.setup_recovery import repair_prompt

        reading = None
        try:
            statuses = source_statuses(cfg, conn)
            if agent_names and conn is not None:
                reading = _reading(cfg, conn)
        finally:
            if conn is not None:
                conn.close()
        if not problems or problems[0][0] != "daemon":
            problems = [
                (name, str(detail)) for name, ok, detail in doctor(cfg) if not ok
            ] + problems
        if failed:
            problems.append(("data sources", f"could not index {', '.join(failed)} (step 4)"))
        if problems:
            verdict = "NO-GO"
            agent_labels = {name for _, name in cli.SETUP_AGENTS}
            connection_failures = [(name, detail) for name, detail in problems
                                   if name in agent_labels]
            summary = (f"{connection_failures[0][0]}: {connection_failures[0][1]}"
                       if connection_failures else
                       f"Not ready: {', '.join(name for name, _ in problems)}.")
        elif agents_connected:
            verdict = "GO"
            summary = "Agents are briefed from this archive."
        else:
            # The archive works, but nothing reads it yet; a plain GO hid that.
            verdict = "GO"
            summary = (
                "The archive is ready. No agent is connected yet: install Claude Code, "
                "Codex or Cursor, then run setup again."
            )
        flag = self._config_flag()
        steps = []
        if agents_changed and agent_names:
            steps.append(f"Restart {' and '.join(agent_names)} so it loads Varyz.")
        if problems:
            steps.append("Copy the repair prompt below and paste it into an AI tool "
                         "on this computer.")
        else:
            steps.append(f'varyz task "build a landing page"{flag}')
        github = next((s for s in statuses if s.info.connector_id == "github"), None)
        if github is not None and not github.connected:
            steps.append(f"varyz connect github{flag}")
        result = {
            "inference_mode": self.inference_mode,
            "cloud_conversations": self.cloud_conversations,
            "verdict": verdict,
            "summary": summary,
            "problems": [[name, detail] for name, detail in problems],
                "repair_prompt": repair_prompt(self.config_path, problems),
            "sources": [
                {
                    "name": status.info.label,
                    "state": status.state,
                    "items": status.items,
                    "last_sync": (
                        cli._local_minute(str(status.last_sync)) if status.last_sync else None
                    ),
                }
                for status in statuses
            ],
            "restart": agents_changed and bool(agent_names),
            "connected_agents": list(agent_names or []),
            "privacy": privacy,
            "next": steps,
            # The last step: the agent's first use of varyz reads the owner's past turns.
            "reading": reading and {**reading, "agents": agent_names,
                                    "restart": agents_changed},
        }
        from varyz.analytics import record

        record(cfg, "setup_ready" if verdict == "GO" else "setup_failed")
        with self._lock:
            self.result = result
        return result


def _reading(cfg: Any, conn: Any) -> dict[str, Any] | None:
    """What the agent's first use of varyz starts reading, or None; never a failed setup."""
    from varyz.resolution.profile_reader import first_reading

    try:
        return first_reading(cfg, conn)
    except Exception:
        return None


class TerminalSetup(SetupRun):
    """The run in this terminal: a bar while a long step runs, its line once it ends."""

    def _set(self, index: int, state: str, detail: str = "") -> None:
        from varyz import cli

        super()._set(index, state, detail)
        if state != "running":
            cli._step(index + 1, STEPS[index], cli._step_state(state, detail))

    def _working_hard(self) -> None:
        from varyz import cli

        cli.console.print(f"[dim]{WARM_NOTE}[/dim]", highlight=False)

    @contextlib.contextmanager
    def _bar(self, index: int | None, description: str) -> Iterator[OnProgress]:
        from varyz import cli

        # Ctrl+C in a download leaves it as `DownloadPaused`, which `varyz`
        # ends with the exit code a paused setup has.
        with cli._progress_bar(description, step=None if index is None else index + 1) as bar:
            yield bar


def print_result(result: dict[str, Any]) -> None:
    """The run's end in the terminal, as the page shows it: problems, verdict, what next."""
    from rich.markup import escape

    from varyz.cli import console

    for name, detail in result["problems"]:
        console.print(f"[red]FAIL[/red] {escape(name)}: {escape(str(detail))}", highlight=False)
    colour = "green" if result["verdict"] == "GO" else "red"
    console.print(f"\n[{colour}]{result['verdict']}[/{colour}]: {escape(result['summary'])}",
                  highlight=False)
    console.print("\nNext:")
    for line in result["next"]:
        console.print(f"  {escape(line)}", highlight=False)
    reading = result.get("reading")
    if reading:
        agents = " or ".join(reading["agents"])
        console.print(f"\nLast step: {'restart' if reading['restart'] else 'open'} {agents} "
                      "and give it any task. It tells you what it will do, then learns how "
                      f"you work from your {reading['turns_waiting']:,} past messages to AI "
                      "agents, in the background. Say \"not now\" to it to pick a later "
                      "time.", highlight=False)


__all__ = ["STEPS", "SetupRun", "Step", "TerminalSetup", "print_result"]
