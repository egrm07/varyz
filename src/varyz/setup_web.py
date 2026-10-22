"""`varyz setup`: the first run as a page in the browser.

Most people who would use Varyz never open a terminal, so `varyz setup` serves
its seven steps -- archive, daemon, agents, data sources, graph, autosync,
classifier -- as one page on 127.0.0.1, where the agents and sources
are ticked rather than answered y/n. Varyz is fully working when it ends.
`varyz setup --terminal` still asks them in the terminal. The double-click
installers in `scripts/` end here.

A download (the search models, Ollama, the classifier's model) can be paused
from the page and resumed, there or by a later `varyz setup`: each keeps what
has arrived.

The page is dressed like the Taste Card (`varyz wrapped`): its colours, its two
typefaces from the bundled cuts, and its bird mark.

What reaches the server is guarded as tightly as a local page can be. It
listens on 127.0.0.1 only, on a port the OS picks. Every request must name
that address in its Host header, so a page elsewhere that rebinds a DNS name
to 127.0.0.1 is refused. The page itself and every API call need a random
token that exists only in the link printed and opened here; another site open
in the same browser cannot read it, and cannot send the custom header that
carries it without a CORS preflight this server never answers.

Consent is exactly what the page showed: the sources offered, with their
paths, are decided once when the page opens, and a run can only approve some
of those.

While the search models download, the page may ask to extract the owner's
preferences from the chats just read (`setup_extract`): what it takes, in
percent of their plan's limits and in minutes, and how long the downloads
still take. A yes starts varyz's own reader in the background; it goes on
after the page is closed.

GitHub is the one source that needs something typed: a token, or the login
the GitHub CLI already holds. The page checks it with GitHub at once, so a
wrong token is said while the owner is still looking at the box, but the
token stays in this process's memory until a run with GitHub ticked puts it in
the OS credential store. It is never sent back to the page.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import subprocess
import threading
import time
import webbrowser
from dataclasses import asdict
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, quote, urlsplit

from varyz.bootstrap import DetectedSources
from varyz.setup_extract import DownloadClock
from varyz.setup_run import STEPS, SetupRun, _Run

if TYPE_CHECKING:
    from varyz.review_web import ReviewServer

PAGE = Path(__file__).resolve().parent / "setup_web.html"
# Ticked when the page opens: the owner's own words, which the briefing is
# built from (the owner's choice, 2026-09-28). Code history and document
# folders are offered unticked.
PRESELECTED_SOURCES = frozenset({"ai_conversations"})
# The card has no colour for a failure; this warm coral is the page's one
# addition to its palette (the owner's choice, 2026-09-28).
FAILED = "#ff7a66"
# The server stops by itself after this long without a request: a tab closed
# without "Close setup" must not leave it behind, even with a run going.
IDLE_LIMIT = 30 * 60.0
# How long a run left by a closed page gets to pause before setup stops anyway.
ABANDON_GRACE = 120.0
# How often the reading's progress is measured again for the page's polls.
EXTRACT_REFRESH = 3.0
# A reading's progress not measured again for this long is shown as old.
EXTRACT_STALE = 60.0
# How often setup notes in its log what it is doing. A setup that stopped
# without a closing line was ended from outside: its window closed, or the
# computer killed it.
LOG_EVERY = 60.0
LOG_LIMIT = 256 * 1024


class SetupLog:
    """``setup.log`` next to the config: what a run did and why it stopped.

    Without it, "Lost touch with setup" on someone else's computer had no
    cause anyone could find. Steps, progress and reasons only: no paths
    beyond the config's, no source content, no credentials.
    """

    def __init__(self, config_path: Path) -> None:
        self.path = Path(config_path).parent / "setup.log"

    def write(self, event: str, detail: str = "") -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {event}"
        if detail:
            line += f": {' '.join(detail.split())[:300]}"
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file() and self.path.stat().st_size > LOG_LIMIT:
                self.path.replace(self.path.with_name("setup.log.1"))
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")

    def progress(self, snapshot: dict[str, Any]) -> None:
        steps = snapshot.get("steps") or []
        running = next((i for i, step in enumerate(steps) if step.get("state") == "running"),
                       None)
        where = f"step {running + 1}" if running is not None else "between steps"
        extract = snapshot.get("extract") or {}
        learning = extract.get("phase") or extract.get("state") or "none"
        self.write("working", f"{where}; {snapshot.get('activity') or ''}; "
                              f"learning {learning} {extract.get('reason') or ''}")


_MAX_BODY = 16 * 1024
_TOKEN_HEADER = "X-Varyz-Setup"


def _github_section(config_path: Path) -> dict[str, Any] | None:
    """The config's [connectors.github] options, or None when there is none."""
    from varyz.bootstrap import _raw_config

    connectors = _raw_config(config_path).get("connectors", {})
    section = connectors.get("github") if isinstance(connectors, dict) else None
    return section if isinstance(section, dict) else None


def _github_account(options: dict[str, Any] | None) -> str | None:
    if not options or options.get("enabled", True) is False:
        return None
    account = str(options.get("account") or "").strip()
    return account or None


def _gh_token() -> str | None:
    from varyz.connectors.github import token_from_gh

    return token_from_gh()


class SetupSession(SetupRun):
    """One browser setup: what was offered, and the run that follows.

    The run is `SetupRun`'s, the terminal's too; this is the page's view of
    it, which the page polls, and the pause and resume it can ask for.
    """

    def __init__(self, config_path: Path, *, detected: DetectedSources | None = None) -> None:
        from varyz import cli
        from varyz.sources import ALPHA_SOURCES

        super().__init__(config_path, detected=detected)
        self.token = secrets.token_urlsafe(24)
        self.activity = ""
        # How much of the running step is done (0 to 1), or None while unknown.
        self.fraction: float | None = None
        # How long the running phase still takes, once its pace is measured.
        self.left = ""
        self._time_left = cli.TimeLeft()
        self.started = False
        self.finished = False
        # The step a download was paused in, which a resume runs again, and
        # whether a pause is asked for and waits for the download's next chunk.
        self.paused: int | None = None
        self.pausing = False
        self.picked: dict[str, list[str]] = {"agents": [], "sources": []}
        self._reads = {info.connector_id: info.reads for info in ALPHA_SOURCES}
        # GitHub: the account already linked, else the one this page linked
        # (kept with its token until a run stores it), and whether the GitHub
        # CLI holds a login the page can offer in one click. Only that it
        # does is kept; its token is read again when the owner asks for it.
        # A section that is paused or has no account is the owner's to fix
        # with `varyz connect github`; the page leaves it alone, as it does
        # every other source the config already names.
        section = _github_section(config_path)
        self.github_account = _github_account(section)
        self.github_offered = section is None or self.github_account is not None
        self.github_cli = section is None and _gh_token() is not None
        # The offer to extract preferences from past chats: None until the
        # sources are read (and when there is nothing to offer), then the
        # offer, the owner's no, or the reading started.
        self._extract: dict[str, Any] | None = None
        self._extract_asked = False
        self._extract_cfg: Any = None
        self._extract_started: dict[str, Any] | None = None
        self._extract_retrying = False
        self._extract_seen: tuple[float, dict[str, Any]] | None = None
        self._extract_lock = threading.Lock()
        self._downloads = DownloadClock()

    # -- what the page reads ------------------------------------------------------

    def plan(self) -> dict[str, Any]:
        from varyz.analytics import status
        from varyz.config import ConfigError, load_config

        # Setup never asks about sharing: the Varyz network popup waits for the
        # owner's first rules review after the trial window (review_web.html).
        sharing = False
        with contextlib.suppress(ConfigError):
            sharing = bool(status(load_config(self.config_path))["consent"])
        return {
            "analytics": {"sharing": sharing},
            "config": str(self.config_path),
            "exists": self.config_path.exists(),
            "ai": self.ai_plan,
            "classifier": self._classifier_model(),
            "agents": [
                {"id": cid, "name": name, "installed": installed}
                for cid, name, installed in self.agents
            ],
            "sources": [
                {
                    "id": cid,
                    "name": name,
                    "reads": self._reads.get(cid, ""),
                    "lines": lines,
                    "checked": cid in PRESELECTED_SOURCES,
                }
                for cid, name, lines in self.offers
            ],
            "github": {
                "account": self.github_login,
                # False while the token is only in this process: a run saves it.
                "saved": self.github_account is not None,
                "reads": self._reads.get("github", ""),
                "cli": self.github_cli,
            } if self.github_offered else None,
        }

    def _classifier_model(self) -> str:
        """The model step 7 downloads; before the first run, the default one."""
        from varyz.config import ConfigError, WorkflowsConfig, load_config

        try:
            return load_config(self.config_path).workflows.classifier_model
        except ConfigError:
            return WorkflowsConfig().classifier_model

    def link_github(self, token: str | None) -> tuple[str | None, str | None]:
        """Check a token with GitHub; (login, None), or (None, what went wrong).

        No token means the GitHub CLI's login. Nothing is stored here: a run
        with GitHub ticked does that.
        """
        import httpx

        from varyz.connectors.github import RateLimited, verify_token

        with self._lock:
            if self.started:
                return None, "setup is already running"
            if not self.github_offered or self.github_account is not None:
                return None, "GitHub is already in this archive's config"
        token = (token or "").strip() or _gh_token()
        if not token:
            return None, "The GitHub CLI has no login here; paste a token instead."
        try:
            login = verify_token(token)
        except ValueError:
            # A 401, or a token GitHub names no account for.
            return None, "GitHub did not accept this token: it is wrong, expired or revoked."
        except RateLimited:
            return None, "GitHub's rate limit for this token is spent; try again within the hour."
        except httpx.HTTPStatusError as exc:
            return None, f"GitHub answered {exc.response.status_code}; try again in a minute."
        except httpx.HTTPError as exc:
            return None, f"Could not reach GitHub ({type(exc).__name__}); check the connection."
        with self._lock:
            if self.started:
                return None, "setup is already running"
            self._github = (login, token)
        return login, None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "started": self.started,
                "finished": self.finished,
                "picked": self.picked,
                "steps": [asdict(step) for step in self.steps],
                "activity": self.activity,
                "fraction": self.fraction,
                "left": self.left,
                "paused": self.paused is not None,
                "pausing": self.pausing,
                "result": self.result,
                "extract": self._extract_view(),
                "downloads": self._downloads_view(),
            }

    def _extract_view(self) -> dict[str, Any] | None:
        # Polling must never wait for archive reads or usage lookups.
        if self._extract_started is None:
            return self._extract
        if self._extract_retrying:
            return {**self._extract, "state": "accepted", "phase": "checking"}
        now = time.time()
        if ((self._extract_seen is None or now - self._extract_seen[0] >= EXTRACT_REFRESH)
                and self._extract_lock.acquire(blocking=False)):
            threading.Thread(target=self._refresh_extract, daemon=True,
                             name="varyz-setup-progress").start()
        if self._extract_seen is None:
            # Not measured yet: the offer's count is no count of what was read.
            return None if self._extract is None else {**self._extract, "turns_left": None}
        seen_at, view = self._extract_seen
        if now - seen_at >= EXTRACT_STALE:
            # A measure that stopped coming is said to be old, not shown as now.
            return {**view, "seconds_left": None, "stale": round(now - seen_at)}
        return view

    def _refresh_extract(self) -> None:
        from varyz import setup_extract

        try:
            started = self._extract_started
            if started is None:
                return
            view = setup_extract.progress(self._extract_cfg, started, time.time())
            from varyz.setup_recovery import repair_prompt

            if view.get("phase") in {"waiting", "failed", "stopped"}:
                view["reason_text"] = setup_extract.plain_reason(
                    str(view.get("reason") or view["phase"]))
                view["repair_prompt"] = repair_prompt(self.config_path, [
                    (self._extract["plan"] + " learning", str(view.get("reason") or view["phase"]))
                ])
            with self._lock:
                if self._extract_started is started:
                    self._extract_seen = (time.time(), {**view, "plan": self._extract["plan"]})
        except Exception:  # noqa: BLE001 - keep the last measured progress
            pass
        finally:
            self._extract_lock.release()

    def _downloads_view(self) -> dict[str, Any] | None:
        """What is downloading and how long all of it still takes; None once none is left.

        Called with ``self._lock`` held.
        """
        from varyz.setup_extract import classifier_download

        if not self.started or self.finished or self.steps[3].state == "waiting":
            return None
        later = None
        model = None
        if self.steps[6].state == "waiting" and self.inference_mode == "local":
            model = self._classifier_model()
            later = classifier_download(model)
        view = self._downloads.view(later, model)
        if not view["now"] and not view["then"]:
            return None
        return view

    # -- extracting preferences ---------------------------------------------------

    def _sources_read(self, run: _Run) -> None:
        """Ask once, if an agent was connected, whether to read the chats now."""
        with self._lock:
            if (self._extract_asked
                    or (not run.agent_names and self.inference_mode != "local")):
                return
            self._extract_asked = True
            self._extract = {"state": "preparing"}
            self._extract_cfg = run.cfg
        # Off the run's thread: the usage is read over the network, and the
        # download must not wait for it.
        threading.Thread(target=self._make_offer, args=(run.cfg,), daemon=True,
                         name="varyz-setup-offer").start()

    def _make_offer(self, cfg: Any) -> None:
        from varyz import setup_extract

        try:
            offered = setup_extract.offer(cfg, self.config_path)
        except Exception:  # noqa: BLE001 - no offer; the agent asks on its first use
            offered = None
        with self._lock:
            if self._extract is None or self._extract.get("state") == "preparing":
                self._extract = offered

    def choose_extract(self, yes: bool) -> str | None:
        """The owner's answer to the offer; an error sentence when there is none to answer."""

        with self._lock:
            offered = self._extract
            if offered is None or offered.get("state") != "offered":
                return "nothing to answer"
            if not yes:
                self._extract = {**offered, "state": "declined"}
                return None
            self._extract = {**offered, "state": "starting"}
        threading.Thread(target=self._begin_extract, args=(offered,), daemon=True,
                         name="varyz-setup-learning").start()
        return None

    def _begin_extract(self, offered: dict[str, Any]) -> None:
        from varyz import setup_extract

        try:
            started = setup_extract.start(self._extract_cfg, self.config_path, offered)
        except Exception:  # noqa: BLE001 - said on the page; the agent asks later
            started = None
        with self._lock:
            if started is None:
                self._extract = {**offered, "state": "unavailable"}
                return None
            self._extract = {**offered, "state": "accepted", "phase": "waiting_model"
                             if offered.get("runner") == "local" else "reading"}
            self._extract_started = started
        return None

    def control_extract(self, action: str) -> str | None:
        """Retry or skip a quota wait; HTTP polling never performs the lookup."""
        with self._lock:
            view = self._extract_seen[1] if self._extract_seen else self._extract or {}
            if (self._extract_started is None
                    or view.get("phase") not in {"waiting", "agent", "failed", "stopped"}
                    or self._extract_retrying):
                return "learning is not waiting for a retry"
            self._extract_retrying = True
        threading.Thread(target=self._control_extract, args=(action,), daemon=True,
                         name="varyz-setup-learning-control").start()
        return None

    def _control_extract(self, action: str) -> None:
        from varyz import setup_extract

        try:
            started = self._extract_started or {}
            if action == "skip":
                if started.get("runner") != "local":
                    # A stopped local reader has nothing to cancel; new prompts restart it.
                    setup_extract.stop(self._extract_cfg)
                with self._lock:
                    self._extract = {**self._extract, "state": "declined"}
                    self._extract_started = None
            else:
                # Keep the saved reading and its diagnostic if retry still cannot start.
                setup_extract.retry(self._extract_cfg, self.config_path,
                                    runner=started.get("runner"))
        except Exception:  # noqa: BLE001 - keep the waiting state and its repair prompt
            pass
        finally:
            with self._lock:
                self._extract_retrying = False
                self._extract_seen = None

    @property
    def extract_accepted(self) -> bool:
        with self._lock:
            return (self._extract_started is not None or (self._extract is not None
                    and self._extract.get("state") in {"starting", "accepted"}))

    @property
    def running(self) -> bool:
        """A run is going: not before it, after it, or while it is paused."""
        with self._lock:
            return self.started and not self.finished and self.paused is None

    # -- the run ------------------------------------------------------------------

    def start(
        self, agents: list[str], sources: list[str], analytics: bool | None = None,
        *, inference_mode: str | None = None, cloud_conversations: bool | None = None,
    ) -> str | None:
        """Start the run in the background; an error sentence if it cannot."""
        offered_agents = {cid for cid, _, installed in self.agents if installed}
        offered_sources = {cid for cid, _, _ in self.offers}
        if self.github_login is not None:
            offered_sources.add("github")
        unknown = sorted((set(agents) - offered_agents) | (set(sources) - offered_sources))
        if unknown:
            return f"not offered on this page: {', '.join(unknown)}"
        mode = inference_mode or self.ai_plan["selected"]
        if not isinstance(mode, str) or mode not in self.ai_plan["choices"]:
            return "this machine cannot comfortably run the local model; use AI conversation mode"
        if cloud_conversations is not None and type(cloud_conversations) is not bool:
            return "cloud_conversations must be true or false"
        with self._lock:
            if self.started:
                return "setup is already running"
            self.analytics_choice = analytics
            self.inference_mode = mode
            if cloud_conversations is not None:
                self.cloud_conversations = cloud_conversations and mode == "conversations"
            elif mode == "local":
                self.cloud_conversations = False
            self.started = True
            self.picked = {"agents": list(agents), "sources": list(sources)}
        threading.Thread(
            target=self.run, args=(tuple(agents), tuple(sources)), daemon=True,
            name="varyz-setup-web",
        ).start()
        return None

    def pause(self) -> str | None:
        """Ask the download under way to stop at its next chunk; an error sentence if none is."""
        from varyz import cli

        with self._lock:
            if not (self.started and not self.finished and self.paused is None):
                return "setup is not running"
            if not cli.is_download(self.activity):
                return "only a download can be paused"
            self.pausing = True
        return None

    def resume(self) -> str | None:
        """Run the paused step again, and the rest after it; an error sentence if it cannot."""
        with self._lock:
            if self.paused is None or self._resume_from is None:
                return "nothing is paused"
            index, run = self.paused, self._resume_from
            self.paused = None
            self.steps[index].state, self.steps[index].detail = "waiting", ""
        threading.Thread(
            target=self._guarded, args=(lambda: self._run_from(index, run),), daemon=True,
            name="varyz-setup-web",
        ).start()
        return None

    def run(self, agents: tuple[str, ...], sources: tuple[str, ...]) -> None:
        """Every step, in order. The page hears of each as it happens."""
        with self._lock:
            self.started = True
        self._guarded(lambda: self.execute(agents, sources))

    def _guarded(self, steps: Any) -> None:
        """Run ``steps``: a pause leaves the run resumable, a failure ends it with NO-GO."""
        from varyz import cli

        paused: int | None = None
        try:
            steps()
        except cli.DownloadPaused:
            with self._lock:
                paused = next(
                    (i for i, step in enumerate(self.steps) if step.state == "running"), None
                )
                if paused is not None:
                    self.steps[paused].state = "paused"
                    self.steps[paused].detail = (
                        f"{self.activity}; what has downloaded is kept"
                    )
        except Exception as exc:  # noqa: BLE001 - the page must say what stopped it
            from varyz.setup_recovery import repair_prompt

            if self._analytics_cfg is not None:
                from varyz.analytics import record

                record(self._analytics_cfg, "setup_failed")
            reason = str(exc) or type(exc).__name__
            with self._lock:
                current = next(
                    (step for step in self.steps if step.state == "running"), None
                )
                if current is not None:
                    current.state = "failed"
                    current.detail = reason
                self.result = {
                    "verdict": "NO-GO",
                    "summary": f"Setup stopped: {reason}",
                    "repair_prompt": repair_prompt(
                        self.config_path, [(current.name if current else "setup", reason)]),
                    "problems": [[current.name if current else "setup", reason]],
                    "sources": [],
                    "restart": False,
                    "privacy": None,
                    "next": [f"varyz setup{self._config_flag()}"],
                }
        finally:
            with self._lock:
                # Said only now, with this run's progress cleared: a resume
                # starting sooner would have its first line wiped by it.
                self.paused = paused
                self.finished = paused is None
                self.pausing = False
                self.activity = ""
                self.fraction = None
                self.left = ""

    def _set(self, index: int, state: str, detail: str = "") -> None:
        from varyz import cli

        with self._lock:
            self.steps[index].state = state
            self.steps[index].detail = detail
            if state == "running":
                # The last step's progress is not this one's.
                self.activity, self.fraction, self.left = "", None, ""
                self._time_left = cli.TimeLeft()
        self._analytics_step(index, state)

    def _progress(self, line: str, fraction: float | None = None) -> None:
        from varyz import cli

        with self._lock:
            if self.pausing:
                if cli.is_download(line):
                    # Raised inside the download, between two chunks it wrote.
                    self.pausing = False
                    raise cli.DownloadPaused(line)
                # The download ended before the pause reached it.
                self.pausing = False
            self.activity = line
            self.fraction = None if fraction is None else min(max(fraction, 0.0), 1.0)
            self.left = self._time_left(line, self.fraction)
            self._downloads.heard(line)

# -- the server ------------------------------------------------------------------


def _fonts() -> dict[str, Path]:
    from varyz.card.fonts import BUNDLED

    fonts = {path.name: path for path in BUNDLED.glob("*.ttf")}
    # Marcellus, the website's display face, lives with the Cloud's web fonts.
    display = Path(__file__).resolve().parent / "cloud" / "fonts" / "marcellus-400.woff2"
    if display.exists():
        fonts[display.name] = display
    return fonts


def font_type(path: Path) -> str:
    return "font/woff2" if path.suffix == ".woff2" else "font/ttf"


def render_page(token: str) -> bytes:
    """The page, with the card's palette, type stacks and mark filled in."""
    from varyz.card import layout
    from varyz.card.fonts import SANS_STACK, SERIF_STACK
    from varyz.card.render import MARK_PATH
    from varyz.onboarding import LEARNING_MESSAGE

    values = {
        "LEARNING_MESSAGE": escape(LEARNING_MESSAGE),
        "TOKEN": json.dumps(token),
        "TOKEN_URL": quote(token, safe=""),
        "MARK_PATH_URL": quote(MARK_PATH),
        "MARK_PATH": MARK_PATH,
        "NIGHT": layout.NIGHT,
        "INK": layout.INK,
        "MUTED": layout.MUTED,
        "DIM": layout.DIM,
        "SIGNAL": layout.SIGNAL,
        "LINE": layout.LINE,
        "FAILED": FAILED,
        "SERIF_STACK": SERIF_STACK,
        "SANS_STACK": SANS_STACK,
    }
    html = PAGE.read_text(encoding="utf-8")
    for name, value in values.items():
        html = html.replace("{{" + name + "}}", value)
    return html.encode("utf-8")


def render_review_error(token: str, message: str) -> bytes:
    """Keep a failed review navigation visible, with an authenticated retry link."""
    prefix = render_page(token).decode("utf-8").split('  <section class="hero">', 1)[0]
    prefix = prefix.replace("<title>Set up Varyz</title>", "<title>Review preferences</title>")
    url_token = quote(token, safe="")
    return (prefix + '<section class="hero"><h1>Review could not open</h1>'
            + f'<p class="line" role="alert">{escape(message)}</p>'
            + f'<div class="actions"><a class="button" href="/review?t={url_token}">'
            + 'Try again</a>'
            + f'<a class="button quiet" href="/?t={url_token}">Back to setup</a></div>'
            + '</section></main></body></html>').encode("utf-8")


def render_cloud_error(token: str, message: str) -> bytes:
    return (render_review_error(token, message).decode("utf-8")
            .replace("<title>Review preferences</title>", "<title>Connect Varyz Cloud</title>")
            .replace("Review could not open", "Cloud could not open")
            .replace('href="/review?', 'href="/cloud?').encode("utf-8"))


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, session: SetupSession, host: str = "127.0.0.1", port: int = 0) -> None:
        self._review: ReviewServer | None = None
        self._review_lock = threading.Lock()
        super().__init__((host, port), _Handler)
        self.session = session
        self.last_request = time.monotonic()
        self.closed = threading.Event()

    @property
    def review_active(self) -> bool:
        from varyz.review_web import IDLE_LIMIT as REVIEW_IDLE_LIMIT

        review = self._review
        return (review is not None and not review.closed.is_set()
                and time.monotonic() - review.last_request < REVIEW_IDLE_LIMIT)

    def open_review(self) -> str:
        """Reuse the normal rules review on this setup's exact config and identity."""
        from varyz.cli import _cfg, _local_owner_caller, _resolution_client
        from varyz.review_web import ReviewServer, ReviewSession

        snapshot = self.session.snapshot()
        extract = snapshot.get("extract") or {}
        if (not snapshot["finished"] or extract.get("state") != "accepted"
                or extract.get("phase") != "done"):
            raise ValueError("Learning is not ready to review yet.")
        with self._review_lock:
            if self.review_active:
                assert self._review is not None
                return self._review.url
            self._close_review()
            cfg = _cfg(self.session.config_path)
            review = ReviewServer(ReviewSession(
                _resolution_client(cfg), _local_owner_caller(cfg, "owner_knowledge"),
                cfg.resolution.device_id, cfg,
            ))
            thread = threading.Thread(target=review.serve_forever,
                                      kwargs={"poll_interval": 0.25}, daemon=True,
                                      name="varyz-setup-review-server")
            thread.start()
            self._review = review
            return review.url

    def open_cloud(self) -> str:
        from varyz.cloud.bridge import ensure_ready

        if not self.session.snapshot()["finished"]:
            raise ValueError("Finish local setup before connecting Cloud.")
        return ensure_ready(self.session.config_path)

    def _close_review(self) -> None:
        if self._review is not None:
            self._review.closed.set()
            self._review.shutdown()
            self._review.server_close()
            self._review = None

    def server_close(self) -> None:
        with self._review_lock:
            self._close_review()
        super().server_close()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{cast(str, host)}:{port}/?t={self.session.token}"

    def allowed_hosts(self) -> set[str]:
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}


_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "font-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


class _Handler(BaseHTTPRequestHandler):
    server: SetupServer
    server_version = "varyz-setup"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        # The launcher's window shows setup's own progress; one line per poll
        # of the page would bury it.
        return

    def _send(
        self, status: HTTPStatus, body: bytes, content_type: str, *, cache: bool = False,
        location: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The token is in the page's own address; no link may carry it away.
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _text(self, status: HTTPStatus, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts()

    def _token_ok(self, given: str | None) -> bool:
        return given is not None and secrets.compare_digest(given, self.server.session.token)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.last_request = time.monotonic()
        if not self._host_ok():
            self._text(HTTPStatus.FORBIDDEN, "wrong host")
            return
        parts = urlsplit(self.path)
        if parts.path == "/":
            token = parse_qs(parts.query).get("t", [None])[0]
            if not self._token_ok(token):
                self._text(
                    HTTPStatus.FORBIDDEN,
                    "Open the link `varyz setup` printed; this one has no key.",
                )
                return
            self._send(HTTPStatus.OK, render_page(self.server.session.token),
                       "text/html; charset=utf-8")
            return
        if parts.path in {"/review", "/cloud"}:
            cloud = parts.path == "/cloud"
            render_error = render_cloud_error if cloud else render_review_error
            token = parse_qs(parts.query).get("t", [None])[0]
            if not self._token_ok(token):
                self._text(HTTPStatus.FORBIDDEN, "missing key")
                return
            try:
                url = self.server.open_cloud() if cloud else self.server.open_review()
            except ValueError as error:
                self._send(HTTPStatus.CONFLICT,
                           render_error(self.server.session.token, str(error)),
                           "text/html; charset=utf-8")
            except Exception:
                message = ("Local Varyz could not connect. Please try again." if cloud
                           else "Review is unavailable. Please try again.")
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, render_error(
                    self.server.session.token, message),
                    "text/html; charset=utf-8")
            else:
                self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", location=url)
            return
        if parts.path.startswith("/fonts/"):
            font = _fonts().get(parts.path.removeprefix("/fonts/"))
            if font is None:
                self._text(HTTPStatus.NOT_FOUND, "no such font")
                return
            self._send(HTTPStatus.OK, font.read_bytes(), font_type(font), cache=True)
            return
        if not self._token_ok(self.headers.get(_TOKEN_HEADER)):
            self._text(HTTPStatus.FORBIDDEN, "missing key")
            return
        if parts.path == "/api/ping":
            self._json({"ok": True})
        elif parts.path == "/api/plan":
            self._json(self.server.session.plan())
        elif parts.path == "/api/state":
            self._json(self.server.session.snapshot())
        else:
            self._text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.last_request = time.monotonic()
        if not self._host_ok():
            self._text(HTTPStatus.FORBIDDEN, "wrong host")
            return
        if not self._token_ok(self.headers.get(_TOKEN_HEADER)):
            self._text(HTTPStatus.FORBIDDEN, "missing key")
            return
        path = urlsplit(self.path).path
        if path == "/api/close":
            self.server.closed.set()
            self._json({"closed": True})
            return
        if path == "/api/review":
            try:
                url = self.server.open_review()
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.CONFLICT)
            except Exception:  # a missing/unavailable daemon must not stop setup
                self._json({"error": "Review is unavailable. Please try again."},
                           HTTPStatus.SERVICE_UNAVAILABLE)
            else:
                self._json({"url": url})
            return
        if path == "/api/github":
            self._post_github()
            return
        if path == "/api/folder-access":
            self._post_folder_access()
            return
        if path == "/api/extract/control":
            body = self._body()
            action = body.get("action") if isinstance(body, dict) else None
            if not isinstance(action, str) or action not in {"retry", "skip"}:
                self._text(HTTPStatus.BAD_REQUEST, "expected action: retry or skip")
                return
            error = self.server.session.control_extract(action)
            self._json({"error": error} if error else {"ok": True},
                       HTTPStatus.CONFLICT if error else HTTPStatus.OK)
            return
        if path == "/api/extract":
            body = self._body()
            answer = body.get("extract") if isinstance(body, dict) else None
            if type(answer) is not bool:
                self._text(HTTPStatus.BAD_REQUEST, "expected {\"extract\": true or false}")
                return
            error = self.server.session.choose_extract(answer)
            if error is not None:
                self._json({"error": error}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True})
            return
        if path in ("/api/pause", "/api/resume"):
            session = self.server.session
            error = session.pause() if path == "/api/pause" else session.resume()
            if error is not None:
                self._json({"error": error}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True})
            return
        if path != "/api/run":
            self._text(HTTPStatus.NOT_FOUND, "not found")
            return
        body = self._body()
        try:
            agents = [str(item) for item in body.get("agents", [])]
            sources = [str(item) for item in body.get("sources", [])]
        except (AttributeError, TypeError):
            body = None
        if body is None:
            self._text(HTTPStatus.BAD_REQUEST, "expected {\"agents\": [...], \"sources\": [...]}")
            return
        sharing = body.get("analytics")
        if sharing is not None and type(sharing) is not bool:
            self._text(HTTPStatus.BAD_REQUEST, "analytics must be true or false")
            return
        mode = body.get("inference_mode")
        cloud = body.get("cloud_conversations")
        if mode is not None and (
            not isinstance(mode, str) or mode not in {"local", "conversations"}
        ):
            self._text(HTTPStatus.BAD_REQUEST, "inference_mode must be local or conversations")
            return
        if cloud is not None and type(cloud) is not bool:
            self._text(HTTPStatus.BAD_REQUEST, "cloud_conversations must be true or false")
            return
        error = self.server.session.start(agents, sources, sharing,
                                         inference_mode=mode, cloud_conversations=cloud)
        if error is not None:
            self._json({"error": error}, HTTPStatus.CONFLICT)
            return
        self._json({"started": True}, HTTPStatus.ACCEPTED)

    def _body(self) -> Any:
        """The request's JSON, or None when it is too long or not JSON."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 <= length <= _MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def _post_folder_access(self) -> None:
        # Which list to open, and the program to add, come from this run's own
        # result, never from the request: the page can only ask for it opened.
        result = self.server.session.snapshot()["result"] or {}
        privacy = result.get("privacy")
        if not privacy:
            self._text(HTTPStatus.CONFLICT, "macOS kept no folder from Varyz")
            return
        from varyz import cli

        try:
            cli._open_folder_access(privacy)
        except (OSError, subprocess.SubprocessError) as exc:
            self._json({"error": f"System Settings did not open: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"opened": True})

    def _post_github(self) -> None:
        # The token comes in and is never sent back; the answer is the login
        # GitHub gave for it, or why there is none.
        body = self._body()
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(body, dict) or not isinstance(token, (str, type(None))):
            self._text(HTTPStatus.BAD_REQUEST, "expected {\"token\": \"...\"} or {}")
            return
        login, error = self.server.session.link_github(token)
        if error is not None:
            self._json({"error": error}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        self._json({"login": login})


def serve(
    config_path: Path,
    *,
    open_browser: bool = True,
    idle_limit: float = IDLE_LIMIT,
) -> bool | None:
    """Serve the setup page until the owner closes it.

    True on GO, False on NO-GO or when setup never ran, and None when it was
    paused: that is not a failure, and `varyz setup` carries on from it.
    """
    from varyz.cli import console
    from varyz.setup_run import print_result

    console.print("[bold]Varyz setup[/bold] · looking for your agents and data sources...")
    session = SetupSession(config_path)
    server = SetupServer(session)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                              daemon=True, name="varyz-setup-server")
    thread.start()
    console.print(f"Setup is open in your browser: {server.url}", highlight=False, soft_wrap=True)
    console.print("[dim]Keep this window open until the page says you are done.[/dim]")
    if open_browser:
        try:
            webbrowser.open(server.url)
        except webbrowser.Error:
            console.print("Could not open a browser; open the link above yourself.")
    abandoned: float | None = None
    log = SetupLog(config_path)
    log.write("opened")
    logged = time.monotonic()
    try:
        # Closing setup must not cut off a review already opened from this page.
        while not server.closed.is_set() or server.review_active:
            time.sleep(1.0)
            if time.monotonic() - logged >= LOG_EVERY and session.started:
                logged = time.monotonic()
                log.progress(session.snapshot())
            idle = time.monotonic() - server.last_request
            if idle > idle_limit and not server.review_active:
                # A closed page with the run still going used to keep this
                # process alive for good, and with it every file of the
                # install. Pause a download so it can carry on later; a run
                # that does not settle within the grace period is left.
                if session.running:
                    if abandoned is None:
                        abandoned = time.monotonic()
                        session.pause()
                    if time.monotonic() - abandoned < ABANDON_GRACE:
                        continue
                    console.print("The setup page was closed while setup was still working; "
                                  f"stopping. Run varyz setup{session._config_flag()} to "
                                  "carry on.")
                    log.write("stopped", f"nothing heard from the page for {idle:.0f} s "
                                         "while setup was working")
                    break
                console.print("Nothing heard from the page for a while; stopping.")
                log.write("stopped", f"nothing heard from the page for {idle:.0f} s")
                break
        else:
            log.write("stopped", "closed from the page")
    except KeyboardInterrupt:
        console.print("Stopped.")
        log.write("stopped", "interrupted in the setup window")
    finally:
        server.shutdown()
        server.server_close()
    snapshot = session.snapshot()
    log.progress(snapshot)
    log.write("steps", "; ".join(f"{i + 1} {step['state']} {step.get('detail') or ''}".strip()
                                 for i, step in enumerate(snapshot["steps"])))
    result = snapshot["result"]
    log.write("result", str(result.get("verdict")) if result else "none")
    if snapshot["paused"]:
        console.print(f"Setup is paused. What has downloaded is kept: run varyz setup"
                      f"{session._config_flag()} to carry on from there.")
        return None
    if result is None and abandoned is not None:
        return False
    if result is None:
        console.print(f"Setup did not run. Start it again any time: varyz setup"
                      f"{session._config_flag()}")
        return False
    if session.extract_accepted:
        # Taken on the page: no agent needs to start it.
        result = {**result, "reading": None}
        console.print("Extracting your preferences from past chats goes on in the background; "
                      "varyz rules read --status says how far it has got.")
    print_result(result)
    return result["verdict"] == "GO"


__all__ = [
    "FAILED", "IDLE_LIMIT", "PRESELECTED_SOURCES", "STEPS", "SetupServer", "SetupSession",
    "serve",
]
