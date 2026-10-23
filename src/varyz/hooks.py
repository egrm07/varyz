"""Agent-client hooks: varyz warms up with the session and briefs the agent itself.

An agent told "call start_task first" sometimes does not. These hooks make the
first step unconditional in clients that run hooks (Claude Code; Codex reads the
same shape):

* ``session-start`` starts the resolution daemon if it is down, without waiting,
  and tells the agent how to work with varyz.
* ``user-prompt`` hands the agent its briefing with the first real task of the
  session (how the owner works, this project's decisions, the definition of
  done), with rule changes delivered on later prompts, including short corrections.
  Every later prompt repeats the owner's writing rules (``writing_rules``): a
  draft often comes from a short follow-up long after the briefing.
* ``stop`` prints nothing by default. A client shows a blocking Stop note to
  the owner under the reply they just read, so the definition of done and the
  writing rules travel in the briefing instead. Two opt-in checks remain:
  ``[agent] done_gate`` sends the agent back once per working checkpoint and
  after edits that no successful finish result followed (a small change is let
  through, ``[agent] done_gate_small_changes``), and ``[agent] writing_check``
  sends back a reply that used a mark an owner rule forbids (an em dash, an
  emoji).
* ``stop`` always starts a background capture of the turn (``varyz.handoff``):
  what was done and what is open, so the next session, on any computer, picks
  up without the owner re-briefing it. The first prompt of every session gets
  that unfinished work as "where you left off", from Cloud when it is set up.

``session-start`` and ``user-prompt`` also start the hourly news check when it is
due, and ``user-prompt`` hands the agent any news post the owner has not seen yet
(see ``varyz.news``) and, once per version, an update an automatic check found
(``varyz.update.agent_notice``).

Every hook fails open: an error, a missing archive or a slow disk prints nothing
and exits 0, so varyz can never block the user's session. The archive is opened
read-only; nothing here writes owner data (the capture runs in its own process).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from varyz.onboarding import GUIDANCE as ONBOARDING_GUIDANCE
from varyz.resolution.memory_policy import MEMORY_POLICY, REALTIME_CORRECTION_REMINDER
from varyz.resolution.questioning import QUESTION_POLICY
from varyz.session_progress import PROGRESS_POLICY

_CONFIG_PATH: Path | None = None  # the --config the hook was installed with
# Set in the reader varyz starts itself (`headless_reader.ENV`): no hook runs in it.
HEADLESS_ENV = "VARYZ_HEADLESS_READER"

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
MIN_TASK_WORDS = 4

SESSION_START_CONTEXT = (
    "varyz (the owner's preference service) is connected. For every task: the "
    "owner's briefing arrives with your first prompt (or call start_task yourself). "
    + QUESTION_POLICY + " Before you build, pass relevant owner-dependent choices "
    "to one `ask` call (questions); put the ask_owner_batch questions "
    "to the user together with your question tool and record each reply with "
    "answer_owner_question (ask whether one holds in all projects only when its scope "
    "block says ask: true, never about the task in hand). Call `ask` with one question "
    "only for a choice that first comes up mid-work. "
    + MEMORY_POLICY + " Call finish_check before you write your "
    "report. varyz talks to you, not to the user: do not narrate your varyz calls or "
    "list its briefings and checklists - report only what is not done or still open."
)
SESSION_START_CONTEXT += "\n\n" + PROGRESS_POLICY


SESSION_START_CONTEXT += "\n\n" + ONBOARDING_GUIDANCE


# -- plumbing ------------------------------------------------------------------


def _read_event() -> dict[str, Any]:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return {}
    value = json.loads(raw.decode("utf-8-sig"))
    return value if isinstance(value, dict) else {}


def _emit(payload: dict[str, Any]) -> None:
    # UTF-8 bytes, not the console code page: on Windows that is cp1252, which
    # cannot encode the briefing's arrows and quotes.
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def _context(event_name: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


def _load_cfg(config: Path | None) -> Any:
    from varyz.config import find_config_file, load_config

    return load_config(find_config_file(config))


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def pause_path(cfg: Any) -> Path:
    """The kill switch: while this file exists every hook does nothing."""
    return cfg.data_dir / "runtime" / "hooks" / "PAUSED"


def _state_path(cfg: Any, session_id: str) -> Path:
    safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return cfg.data_dir / "runtime" / "hooks" / f"{safe}.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8", newline="\n")


def _project(event: dict[str, Any]) -> tuple[str | None, Path | None]:
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None, None
    folder = Path(cwd)
    return folder.name or None, folder


def task_text(prompt: str) -> str:
    """The task in a prompt: a leading slash command's name is not part of it."""
    text = " ".join(prompt.split())
    if text.startswith("/"):
        _command, _, text = text.partition(" ")
    return text


# Prompts the client writes itself (background-task notices, reminders, local
# command output) arrive through the same hook as the owner's; they are not tasks.
_CLIENT_GENERATED = re.compile(r"\s*(?:<[a-z][a-z0-9_-]*[ >]|\[SYSTEM NOTIFICATION)")


# A plain question asks for an answer, not work: "do I need to …?", "why is …".
# A request phrased as one ("can you add a pricing page?") is still a task.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WH_START = re.compile(r"(?:what|why|how|where|when|which|who|whose)\b", re.IGNORECASE)
_REQUEST = re.compile(
    r"(?:please\b|(?:can|could|would|will) (?:you|we)\b.*\b(?:build|make|create|add|fix|"
    r"write|implement|refactor|update|ship|deploy|set up|change|remove|delete|rename|move|"
    r"polish|design|run|test|migrate|install|connect|convert|generate|clean|improve|"
    r"finish|port|wire|redo|rewrite)\b)",
    re.IGNORECASE,
)


def is_question(prompt: str) -> bool:
    """Every sentence only asks something; none asks for work to be done.

    Filler shorter than three words ("yea", "ok thanks") is neither. A statement or
    an instruction anywhere makes the prompt a task.
    """
    asked = False
    for sentence in _SENTENCE_END.split(task_text(prompt)):
        sentence = sentence.strip()
        if not sentence:
            continue
        asks = sentence.endswith("?") or bool(_WH_START.match(sentence))
        if not asks and len(sentence.split()) < 3:
            continue
        if not asks or _REQUEST.match(sentence):
            return False
        asked = True
    return asked


def _asks(sentence: str) -> bool:
    return (
        (sentence.endswith("?") or bool(_WH_START.match(sentence)))
        and not _REQUEST.match(sentence)
    )


def task_focus(prompt: str) -> str:
    """The part of a prompt that asks for work.

    In "skip plain questions. also should we use an llm?" the work is the first
    sentence; the question must not add an AI-integration checklist to it.
    """
    text = task_text(prompt)
    sentences = [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]
    work = [sentence for sentence in sentences if not _asks(sentence)]
    return " ".join(work) if work and len(work) < len(sentences) else text


def is_task(prompt: str, classifier: Any = None) -> bool:
    """A prompt that asks for work. ``classifier`` (text -> Verdict) replaces the
    wording rule for prompts long enough to be a task; client notices never are."""
    if _CLIENT_GENERATED.match(prompt):
        return False
    if len(task_text(prompt).split()) < MIN_TASK_WORDS:
        return False
    if classifier is not None:
        return bool(classifier(task_text(prompt)).is_a)
    return not is_question(prompt)


# -- events --------------------------------------------------------------------


def session_start(cfg: Any, event: dict[str, Any]) -> dict[str, Any] | None:
    from varyz.resolution.away import RECOGNITION
    with contextlib.suppress(Exception):
        from varyz.install import refresh_managed_instructions

        refresh_managed_instructions(_config_file())
    resolution = cfg.resolution
    if resolution.autostart and resolution.bind_host in {"127.0.0.1", "localhost", "::1"}:
        with contextlib.suppress(Exception):
            _start_daemon_if_down(resolution.bind_host, resolution.port, _config_file())
    if cfg.agent.briefing_hooks:
        with contextlib.suppress(Exception):
            from varyz.resolution.prompt_kind import warm_for_session

            warm_for_session(cfg)
    with contextlib.suppress(Exception):
        from varyz.news import maybe_check_on_use

        maybe_check_on_use(cfg, _config_file())
    with contextlib.suppress(Exception):
        # Warm the copy of Cloud's open work that the first briefing reads.
        from varyz.handoff import warm

        project = _project(event)[0]
        if project:
            warm(cfg, _config_file(), project)
    session_id = str(event.get("session_id") or "")
    if session_id:
        # The first briefing then leaves out the policy this note already gives.
        with contextlib.suppress(OSError):
            state_file = _state_path(cfg, session_id)
            _save_state(state_file, {**_load_state(state_file), "policy_shown": True})
    return _context("SessionStart", SESSION_START_CONTEXT + "\n\n" + RECOGNITION)


def _config_file() -> Path | None:
    from varyz.config import find_config_file

    path = find_config_file(_CONFIG_PATH)
    return path.resolve() if path is not None else None


def _start_daemon_if_down(host: str, port: int, config_file: Path | None) -> None:
    if config_file is not None and config_file.is_file():
        from varyz.config import load_config
        from varyz.resolution.availability import is_paused

        if is_paused(load_config(config_file)):
            return
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return
    except OSError:
        pass
    command = [sys.executable, "-m", "varyz", "daemon", "run"]
    if config_file is not None:
        command += ["--config", str(config_file)]
    # A hidden console, not DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW
    # beside it, and a daemon with no console makes every git or gh it runs
    # open a terminal window of its own.
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(  # noqa: S603 - fixed local module invocation
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
        start_new_session=sys.platform != "win32",
    )


def user_prompt(cfg: Any, event: dict[str, Any]) -> dict[str, Any] | None:
    """The briefing for this prompt, with any news post the owner has not seen."""
    from varyz.onboarding import GUIDANCE, is_request, note_conversation

    prompt = event.get("prompt")
    session_id = str(event.get("session_id") or "")
    if cfg.agent.briefing_hooks and isinstance(prompt, str) and not _CLIENT_GENERATED.match(prompt):
        note_conversation(cfg, session_id, onboarding=is_request(prompt, cfg))
        if is_request(prompt, cfg):
            return _context("UserPromptSubmit", GUIDANCE + "\n"
                            + (f"Use conversation_id {session_id!r}. " if session_id else "")
                            + "Call start_task now.")
    prompt = event.get("prompt")
    if isinstance(prompt, str) and not _CLIENT_GENERATED.match(prompt):
        nudge_live_reading(cfg)
    session_id = str(event.get("session_id") or "")
    was_briefed = bool(session_id and _load_state(_state_path(cfg, session_id)).get("briefed"))
    result = _briefing_for_prompt(cfg, event)
    if not cfg.agent.briefing_hooks or not isinstance(prompt, str):
        return result
    if _CLIENT_GENERATED.match(prompt):
        return result
    # Changes in owner rules matter even for short corrections and questions that
    # the task classifier skips. Never wait for a new work domain or session.
    with contextlib.suppress(Exception):
        updates = _preference_updates(cfg, event, was_briefed=was_briefed)
        if updates:
            previous = (result or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
            result = _context("UserPromptSubmit", _joined(previous, updates))
    return _with_news(cfg, result, conversation_id=str(event.get("session_id") or "") or None)


def _preference_updates(cfg: Any, event: dict[str, Any], *, was_briefed: bool) -> str:
    """Deliver changed rules independently of task classification and ranking.

    The archive stays read-only. Only delivery receipts go into hook session state;
    interpreting and saving an owner's correction remains the working agent's job.
    """
    from varyz.resolution.briefing import _rule_row
    from varyz.resolution.principles import GLOBAL_SCOPE, SQLitePrincipleStore, normalize_scope

    session_id = str(event.get("session_id") or "")
    if not session_id or not cfg.db_path.exists():
        return ""
    state_file = _state_path(cfg, session_id)
    state = _load_state(state_file)
    if not state.get("briefed"):
        return ""
    project, _cwd = _project(event)
    scope = normalize_scope(project) if project else None
    domains = set(state.get("domains", []))
    with contextlib.closing(_open_readonly(cfg.db_path)) as conn:
        store = SQLitePrincipleStore(conn)
        rules = [p for p in store.current() if p.scope in {GLOBAL_SCOPE, scope}
                 and (not p.domain or p.domain in domains)]
    current = {p.id: p.statement for p in rules}
    previous = state.get("preference_snapshot")
    text = ""
    if previous is None and was_briefed:
        # Upgrade an existing chat: it has never received delivery receipts.
        # Show current constraints once rather than silently baselining them.
        previous = {}
    if isinstance(previous, dict):
        added = [p for p in rules if previous.get(p.id) != p.statement]
        removed = [rule_id for rule_id in previous if rule_id not in current]
        if added or removed:
            text = "Varyz owner rule updates: apply these before continuing work.\n"
            text += "\n".join(_rule_row(p.advice()) for p in added)
            if removed:
                text += "\nNo longer active in this task (do not keep following stale versions): "
                text += ", ".join(removed)
    state["preference_snapshot"] = current
    _save_state(state_file, state)
    return _joined(text, REALTIME_CORRECTION_REMINDER)


def _with_news(cfg: Any, result: dict[str, Any] | None, *,
               conversation_id: str | None = None) -> dict[str, Any] | None:
    """Adds unseen news posts, and an update a `notify` check found, for the owner."""
    from varyz import news, update

    notices: list[str] = []
    learning_failure = None
    with contextlib.suppress(Exception):
        from varyz.onboarding import learning_notice

        learning_failure = learning_notice(cfg)
        if learning_failure:
            notices.append(learning_failure)
    with contextlib.suppress(Exception):
        news.maybe_check_on_use(cfg, _config_file())
    with contextlib.suppress(Exception):
        if found := update.agent_notice(cfg):
            notices.append(found)
    wrapped = None
    with contextlib.suppress(Exception):
        if not learning_failure:
            from varyz import wrapped_monthly

            wrapped = wrapped_monthly.agent_notice(cfg, conversation_id=conversation_id)
    # A Wrapped month holds the news posts for the next prompt: one line, not more.
    with contextlib.suppress(Exception):
        if not learning_failure and not wrapped and (found := news.agent_notice(
                cfg, conversation_id=conversation_id)):
            notices.append(found)
    if wrapped:
        notices.append(wrapped)  # Last, so it ends the reply.
    if not notices:
        return result
    notice = "\n\n".join(notices)
    if result is None:
        return _context("UserPromptSubmit", notice)
    output = result["hookSpecificOutput"]
    briefing = str(output.get("additionalContext", ""))
    output["additionalContext"] = f"{briefing}\n\n{notice}".strip()
    return result


def nudge_live_reading(cfg: Any) -> None:
    """Have the daemon read the AI chats' new messages into the archive (`resolution.live`).

    Best effort, and never waits: the hook does not start the daemon for it.
    """
    if not cfg.workflows.live_reading:
        return
    from varyz.resolution.client import ResolutionClient

    resolution = cfg.resolution
    if not _port_open(resolution.bind_host, resolution.port):
        return
    with contextlib.suppress(Exception):
        ResolutionClient(
            resolution.bind_host, resolution.port, auth_mode=resolution.auth_mode,
            archive=cfg.db_path, timeout=2.0,
        ).nudge_live()


def _reading_offer(cfg: Any, conn: Any, prompt: str, *,
                   daily: bool) -> dict[str, Any] | None:
    """The reader's ask for this briefing, or None; never a briefing lost to it.

    When varyz can start the reader itself (`headless_reader`), it does, and the
    agent is asked for nothing.
    """
    from varyz.resolution.headless_reader import hand_off
    from varyz.resolution.profile_reader import agent_reading_offer

    try:
        now = time.time()
        offer = agent_reading_offer(cfg, conn, now, prompt=prompt, daily=daily)
        return hand_off(cfg, offer, _config_file(), now)
    except Exception:
        return None


def _live_job_text(cfg: Any, prompt: str) -> str:
    """The live reader's ask for a prompt that gets no briefing, or ''."""
    from varyz.resolution.briefing import render_background_job

    if not cfg.db_path.exists():
        return ""
    with contextlib.closing(_open_readonly(cfg.db_path)) as conn:
        job = _reading_offer(cfg, conn, prompt, daily=False)
    return render_background_job(job) if job else ""


def _joined(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def _briefing_for_prompt(cfg: Any, event: dict[str, Any]) -> dict[str, Any] | None:
    from varyz.resolution.away import RECOGNITION
    from varyz.resolution.away import status as away_status
    from varyz.resolution.briefing import FINISH_REMINDER, render_markdown, start_task
    from varyz.resolution.prompt_kind import prompt_classifier_from_config
    from varyz.resolution.task_size import sizer_from_config

    if not cfg.agent.briefing_hooks:
        return None
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or _CLIENT_GENERATED.match(prompt):
        return None
    project, cwd = _project(event)
    session_id = str(event.get("session_id") or "")
    mode_text = ""
    mode: dict[str, Any] = {"active": False}
    if cfg.db_path.exists():
        with contextlib.closing(_open_readonly(cfg.db_path)) as mode_conn:
            mode = away_status(mode_conn, project, session_id or None)
        mode_text = (
            f"Varyz conversation_id: {session_id or 'create a unique task id once'}. "
            "Reuse it for away, ask, start_task and finish_check. "
            + (mode["how"] if mode["active"] else "Away mode is not active for this task.")
        )
    # Recognition stays with the working agent even when the local task classifier
    # calls a short control message (including /away or 'I'm back') a non-task.
    if prompt.strip().startswith("/away"):
        return _context("UserPromptSubmit", mode_text + "\n" + RECOGNITION)
    if not is_task(prompt, prompt_classifier_from_config(cfg)):
        # "continue" on a new computer is exactly when the earlier work is needed.
        session_task = (_load_state(_state_path(cfg, session_id)).get("task")
                        if session_id else None)
        text = _joined(mode_text if mode.get("active") else "", _live_job_text(cfg, prompt),
                       _left_off(cfg, project, session_id, prompt),
                       _writing_reminder(cfg, project, cwd, prompt, session_task))
        return _context("UserPromptSubmit", text) if text else None
    task = task_focus(prompt)
    state_file = _state_path(cfg, session_id) if session_id else None
    state = _load_state(state_file) if state_file else {}
    first = not state.get("briefed")
    session_task = state.get("task")

    if not cfg.db_path.exists():
        return None
    conn = _open_readonly(cfg.db_path)
    try:
        briefing = start_task(
            conn, task[:8000], project=project, cwd=cwd, sizer=sizer_from_config(cfg),
            conversation_id=session_id or None,
            cfg=cfg,
        )
        if briefing.get("status") == "delegated":
            return None
        from varyz.cloud.local_access import briefing_notice

        cloud_access = briefing_notice(_config_file())
        if cloud_access:
            briefing["cloud_access"] = cloud_access
        # The reader's ask rides on the briefing every session gets, not only
        # on a start_task the agent would call itself: the full reading's on a
        # session's first prompt, the live reading's on any.
        job = _reading_offer(cfg, conn, prompt, daily=first)
        if job is not None:
            briefing["background_job"] = job
        with contextlib.suppress(Exception):
            from varyz.resolution.shadow import observe_briefing

            observe_briefing(conn, cfg, task[:8000], project, session_id or None,
                             briefing, origin="hook")
    finally:
        conn.close()
    _record_trial_uses(cfg, briefing, session_id or None, task[:8000])
    domains = [item["domain"] for item in briefing["kinds_of_work"]]
    seen = set(state.get("domains", []))
    new_domains = [name for name in domains if name not in seen]

    state.update({
        "briefed": True,
        "domains": sorted(seen | set(domains)),
        "task": task[:8000],
        "project": project,
        "cwd": str(cwd) if cwd else None,
    })
    if state_file is not None:
        with contextlib.suppress(OSError):
            _save_state(state_file, state)

    if first:
        text = (
            "varyz briefing for this task (start_task was run for you; its result "
            "follows). Treat it as the owner's constraints.\n\n"
            + render_markdown(briefing, policy_known=bool(state.get("policy_shown")))
        )
        return _context("UserPromptSubmit",
                        _joined(text, _left_off(cfg, project, session_id, task)))
    if new_domains:
        partial = {
            **briefing,
            "how_owner_works": [],
            "project_vision": [],
            "project_rules": [],
            "project_decisions": [],
            "general_preferences": [],
            "pending_owner_questions": [],
            "definition_of_done": {
                name: items
                for name, items in briefing["definition_of_done"].items()
                if name in new_domains
            },
            "ask_varyz_about": [
                item for item in briefing["ask_varyz_about"] if item["domain"] in new_domains
            ],
        }
        text = (
            "varyz: this prompt adds a new kind of work. The owner's expectations "
            "for it:\n\n" + render_markdown(partial, include_protocol=False)
        )
        return _context("UserPromptSubmit", _joined(text, FINISH_REMINDER))
    from varyz.resolution.briefing import render_background_job

    text = _joined(
        mode_text if mode.get("active") else "",
        render_background_job(job) if job is not None else "",
        render_markdown({"project": project, "history_check": briefing["history_check"]},
                        include_protocol=False),
        _writing_reminder(cfg, project, cwd, prompt, session_task),
        FINISH_REMINDER,
    )
    return _context("UserPromptSubmit", text) if text else None


def _writing_reminder(
    cfg: Any, project: str | None, cwd: Path | None, prompt: str, session_task: Any
) -> str:
    """The owner's writing rules, again on every later prompt: a draft often comes
    from a short follow-up ("make it kinder") long after the briefing. Rules for
    emails and messages ride along when the prompt or the session's task involves
    one. '' when there are none or the archive cannot be read."""
    if not cfg.db_path.exists():
        return ""
    from varyz.resolution.domains import detect_domains
    from varyz.resolution.knowledge import normalize_scope
    from varyz.resolution.principles import SQLitePrincipleStore
    from varyz.resolution.writing_rules import reminder, select_writing

    context = [session_task] if isinstance(session_task, str) else []
    try:
        with contextlib.closing(_open_readonly(cfg.db_path)) as conn:
            found = select_writing(
                SQLitePrincipleStore(conn).current("confirmed"), task_text(prompt)[:8000],
                project=project, domains=detect_domains(" ".join([prompt, *context]), cwd),
                context=context, normalize_scope=normalize_scope,
            )
    except (sqlite3.Error, OSError):
        return ""
    return reminder(found)


def _left_off(cfg: Any, project: str | None, session_id: str, task: str) -> str:
    """Unfinished work from earlier sessions, once per session; '' if none or unknown."""
    if not project or not session_id:
        return ""
    state_file = _state_path(cfg, session_id)
    state = _load_state(state_file)
    if state.get("left_off_shown"):
        return ""
    try:
        from varyz.handoff import open_work, render

        text = render(open_work(cfg, _config_file(), project=project, task=task[:240],
                                conversation_id=session_id, cloud="cached"))
    except Exception:
        return ""
    state["left_off_shown"] = True
    with contextlib.suppress(OSError):
        _save_state(state_file, state)
    return text


def _capture_turn(cfg: Any, event: dict[str, Any]) -> None:
    """Journal this turn in the background, whether or not the agent did."""
    from varyz.handoff import can_capture, capture_in_background

    session_id = str(event.get("session_id") or "")
    transcript = event.get("transcript_path")
    if not session_id or not isinstance(transcript, str) or not transcript:
        return
    project = _project(event)[0] or _load_state(_state_path(cfg, session_id)).get("project")
    if isinstance(project, str) and project and can_capture(cfg):
        capture_in_background(_config_file(), project, session_id, transcript)


def edited_since_last_prompt(transcript_path: str | None) -> bool | None:
    """Whether the agent changed files after the user's last prompt; None if unknown."""
    activity = turn_activity(transcript_path)
    return None if activity is None else activity[0]


def turn_activity(transcript_path: str | None) -> tuple[bool, bool] | None:
    """(edited, checked) since the user's last prompt; None if unknown.

    Supports Claude transcripts and native Codex rollouts. A call is not a
    successful check: only its matching current-generation result counts.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    edited = checked = False
    generation = 0
    finish_ids: dict[str, int] = {}
    latest_finish: str | None = None
    canonical = False
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if entry.get("type") == "response_item" and isinstance(payload, dict):
            canonical = True
            kind = payload.get("type")
            if kind == "message":
                if payload.get("role") != "user":
                    continue
                entry = {"type": "user", "message": {"content": [
                    {"type": "text", "text": b.get("text", "")}
                    for b in payload.get("content", []) if isinstance(b, dict)
                    and b.get("type") in {"input_text", "text"}
                ]}}
            elif kind in {"function_call", "custom_tool_call"}:
                entry = {"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "name": payload.get("name"),
                    "id": payload.get("call_id"),
                }]}}
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                entry = {"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": payload.get("call_id"),
                    "content": payload.get("output"), "is_error": payload.get("is_error"),
                }]}}
            else:
                continue
        elif entry.get("type") == "event_msg" and isinstance(payload, dict):
            # Modern rollouts duplicate user messages as UI events. Their later
            # arrival must not erase work already present in response items.
            if canonical or payload.get("type") != "user_message":
                continue
            entry = {"type": "user", "message": {"content": payload.get("message")}}
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if entry.get("type") == "user":
            # A real prompt resets the turn; a tool result is part of the turn, and
            # so is a client note such as an image caption (isMeta).
            if entry.get("isMeta"):
                continue
            if isinstance(content, str) or (
                isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
                and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                )
            ):
                edited = checked = False
                generation += 1
                finish_ids.clear()
                latest_finish = None
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    identity = block.get("tool_use_id")
                    if not isinstance(identity, str):
                        continue
                    if (identity != latest_finish or identity not in finish_ids
                            or finish_ids[identity] != generation):
                        continue
                    outcome = _completion_ready(block.get("content"))
                    if block.get("is_error") or outcome is False:
                        checked = False
                    elif outcome is True:
                        checked = True
        elif entry.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                short_name = (name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
                              if isinstance(name, str) else "")
                if short_name in EDIT_TOOLS:
                    edited, checked = True, False
                    generation += 1
                    latest_finish = None
                elif isinstance(name, str) and name.endswith("finish_check"):
                    # mcp__varyz__finish_check in Claude Code; the suffix in any client
                    checked = False
                    latest_finish = block.get("id")
                    if isinstance(block.get("id"), str):
                        finish_ids[block["id"]] = generation
    return edited, checked


def _completion_ready(value: Any, depth: int = 0) -> bool | None:
    """Read only an explicit result flag, through bounded MCP content wrappers."""
    if depth > 5:
        return None
    if isinstance(value, str):
        try:
            return _completion_ready(json.loads(value), depth + 1)
        except ValueError:
            return None
    if isinstance(value, list):
        for item in value:
            result = _completion_ready(item, depth + 1)
            if result is not None:
                return result
    if isinstance(value, dict):
        if value.get("isError") or value.get("is_error"):
            return False
        completion = value.get("task_completion")
        if isinstance(completion, dict) and (
            completion.get("continue_work") is True or completion.get("ready_to_report") is False
        ):
            return False
        verification = value.get("verification")
        if isinstance(verification, dict) and isinstance(verification.get("ready_to_report"), bool):
            return verification["ready_to_report"]
        for key in ("content", "text", "structuredContent"):
            if key in value:
                result = _completion_ready(value[key], depth + 1)
                if result is not None:
                    return result
    return None


def stop(cfg: Any, event: dict[str, Any]) -> dict[str, Any] | None:
    # The turn is in the transcript now: what the owner said in it is read at once.
    nudge_live_reading(cfg)
    with contextlib.suppress(Exception):
        _capture_turn(cfg, event)
    if event.get("stop_hook_active"):
        return None
    breach = ""
    if cfg.agent.writing_check:
        with contextlib.suppress(Exception):
            breach = _writing_breach(cfg, event)
    gate = _done_gate(cfg, event) if cfg.agent.done_gate else None
    if not breach:
        return gate
    reason = breach if gate is None else breach + "\n\n" + str(gate["reason"])
    return {"decision": "block", "reason": reason}


def last_reply(transcript_path: str | None) -> str:
    """Everything the agent wrote to the user since their last prompt; '' if unknown.

    Reads Claude transcripts and native Codex rollouts, like ``turn_activity``.
    """
    if not transcript_path:
        return ""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    texts: list[str] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if entry.get("type") == "response_item" and isinstance(payload, dict):
            if payload.get("type") != "message":
                continue
            blocks = payload.get("content")
            if payload.get("role") == "user":
                texts.clear()
            elif payload.get("role") == "assistant" and isinstance(blocks, list):
                texts.extend(str(b.get("text", "")) for b in blocks if isinstance(b, dict)
                             and b.get("type") in {"output_text", "text"})
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if entry.get("type") == "user" and not entry.get("isMeta"):
            if isinstance(content, str) or (
                isinstance(content, list)
                and not any(isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content)
            ):
                texts.clear()
        elif entry.get("type") == "assistant" and isinstance(content, list):
            texts.extend(str(b.get("text", "")) for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return "\n".join(texts)


def _writing_breach(cfg: Any, event: dict[str, Any]) -> str:
    """Why the reply goes back: it used a mark an owner rule forbids. Once per
    reply; '' when it is clean, no rule forbids a mark, or nothing can be read."""
    from varyz.resolution.knowledge import normalize_scope
    from varyz.resolution.principles import SQLitePrincipleStore
    from varyz.resolution.writing_rules import breaches, forbidden_marks, select_writing

    reply = last_reply(event.get("transcript_path"))
    last = event.get("last_assistant_message")
    if isinstance(last, str) and last not in reply:
        reply = _joined(reply, last)
    session_id = str(event.get("session_id") or "")
    if not reply.strip() or not session_id or not cfg.db_path.exists():
        return ""
    state_file = _state_path(cfg, session_id)
    state = _load_state(state_file)
    project = _project(event)[0] or state.get("project")
    task = str(state.get("task") or "")
    with contextlib.closing(_open_readonly(cfg.db_path)) as conn:
        rules = select_writing(SQLitePrincipleStore(conn).current("confirmed"), task,
                               project=project, normalize_scope=normalize_scope).text
    marks = forbidden_marks(rules)
    found = breaches(reply, marks)
    if not found:
        return ""
    key = hashlib.sha256(reply.encode("utf-8")).hexdigest()[:16]
    if state.get("writing_checked") == key:
        return ""
    state["writing_checked"] = key
    with contextlib.suppress(OSError):
        _save_state(state_file, state)
    statements = {rule.id: rule.statement for rule in rules}
    used = " and ".join("an em dash" if mark == "em dash" else "an emoji" for mark in found)
    lines = [f"varyz writing check. Your reply used {used}, which the owner's rules forbid:"]
    lines += [f"- {statements[marks[mark]]} (id {marks[mark]})" for mark in found]
    lines.append(
        "The owner has already read the reply. If the mark was in a draft, email, message "
        "or other text the owner will send or publish, give that text again, corrected, "
        "with no apology and no note about this check. If it was only in your own "
        "explanation, or in text you quoted from someone else, end the turn without "
        "writing anything more. Follow these rules in everything you write for the rest "
        "of this session."
    )
    return "\n".join(lines)


def _done_gate(cfg: Any, event: dict[str, Any]) -> dict[str, Any] | None:
    from varyz.resolution.briefing import finish_check
    from varyz.resolution.task_size import sizer_from_config

    session_id = str(event.get("session_id") or "")
    if not session_id:
        return None
    state_file = _state_path(cfg, session_id)
    state = _load_state(state_file)
    task = state.get("task")
    if not isinstance(task, str) or not task:
        return None
    task_key = hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]
    activity = turn_activity(event.get("transcript_path"))
    if not cfg.db_path.exists():
        return None
    cwd = Path(state["cwd"]) if state.get("cwd") else None
    conn = _open_readonly(cfg.db_path)
    try:
        check = finish_check(
            conn, task, project=state.get("project"), cwd=cwd,
            sizer=sizer_from_config(cfg), conversation_id=session_id, cfg=cfg,
        )
        with contextlib.suppress(Exception):
            from varyz.resolution.shadow import observe_briefing

            observe_briefing(conn, cfg, task, state.get("project"), session_id,
                             check, origin="hook", phase="finish_check")
    finally:
        conn.close()
    if check.get("status") == "delegated":
        return None
    completion = check.get("task_completion", {})
    continue_work = completion.get("continue_work") is True
    reference = completion.get("reference")
    # Re-prompt once for the CURRENT checkpoint, rather than permanently using
    # the first turn's task text as a completion receipt. A genuine blocked
    # handoff never becomes an automatic continuation.
    pending_gate = continue_work and reference and state.get("continued") != reference
    standards_gate = bool(activity and activity[0] and not activity[1]
                          and state.get("gated") != task_key)
    if not pending_gate:
        if not standards_gate:
            return None
        if (cfg.agent.done_gate_small_changes == "skip"
                and _small_change(cfg, state, task_key, task)):
            with contextlib.suppress(OSError):
                _save_state(state_file, state)
            return None
    items = check["definition_of_done"]
    if not pending_gate and not items:
        return None
    if pending_gate:
        state["continued"] = reference
    if standards_gate:
        state["gated"] = task_key
    with contextlib.suppress(OSError):
        _save_state(state_file, state)
    # The Stop hook runs after the report is on the user's screen, and what is
    # shown cannot be taken back. So the agent is told not to write it again:
    # the owner reads one report, and at most a short addendum under it.
    lines = [
        "varyz finish check. The owner already has your report: do not repeat it or "
        "mention this check. Finish any authorised work left below, then add at most "
        "one short line on what is still open, or say the checklist is met. If only "
        "owner input or an outside blocker remains, record a blocked checkpoint.",
    ]
    if pending_gate:
        lines.append("Current task: " + completion["task"])
        lines.append("Unfinished (agent-reported):")
        lines += ["- " + item for item in completion.get("pending", [])]
    lines += [f"- {item['item']}" for item in items]
    if check.get("away_guidance"):
        lines.append(check["away_guidance"])
    if check["open_owner_questions"]:
        lines.append("Still unanswered in this conversation:")
        lines += [f"- {item['question']}" for item in check["open_owner_questions"]]
    return {"decision": "block", "reason": "\n".join(lines)}


def _small_change(cfg: Any, state: dict[str, Any], task_key: str, task: str) -> bool:
    """The local model, already loaded, calls the task a small change. Asked once
    per task; a cold or absent model never lets the finish check be skipped."""
    from varyz.resolution.local_classifier import ClassifierUnavailable
    from varyz.resolution.task_size import gate_sizer_from_config

    sized = state.get("sized")
    if isinstance(sized, list) and len(sized) == 2 and sized[0] == task_key:
        return sized[1] == "small"
    sizer = gate_sizer_from_config(cfg)
    if sizer is None:
        return False
    try:
        size = sizer(task, ()).size
    except ClassifierUnavailable:
        return False
    state["sized"] = [task_key, size]
    return size == "small"


HANDLERS = {"session-start": session_start, "user-prompt": user_prompt, "stop": stop}


# -- doctor --------------------------------------------------------------------


def doctor(
    cfg: Any, *, client_configs: dict[str, Path] | None = None, quick: bool = False
) -> list[tuple[str, bool, str]]:
    """Go/no-go for the owner workflow: (check, ok, detail) per line, counts only."""
    from varyz import install

    checks: list[tuple[str, bool, str]] = []
    checks.append(("daemon", *_daemon_check(cfg)))
    if cfg.db_path.exists():
        conn = _open_readonly(cfg.db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(owner_principles)")}
            migrated = "domain" in columns
            standards = (
                conn.execute(
                    "SELECT COUNT(*) FROM owner_principles"
                    " WHERE domain != '' AND source != 'standards_seed'"
                ).fetchone()[0]
                if migrated
                else 0
            )
            from varyz.sources import connected_summary, source_statuses

            # Informational, never a NO-GO: rules alone brief an agent, and
            # an owner who connected nothing should still see GO. What they
            # could not see before was that nothing was connected.
            sources = connected_summary(source_statuses(cfg, conn))
        finally:
            conn.close()
        checks.append(("database", migrated, "scope and kinds of work" if migrated
                       else "run `varyz daemon start` to migrate"))
        # Informational: varyz ships no rules of its own, so an owner has
        # none by kind of work until they keep some.
        checks.append(("definitions of done", True,
                       f"{standards} rules by kind of work" if standards
                       else "none yet: keep some in `varyz rules review`"))
        checks.append(("data sources", True, sources))
    else:
        checks.append(("database", False, f"missing: {cfg.db_path}"))
    checks.append(("hooks enabled", bool(cfg.agent.briefing_hooks),
                   "stop check on" if cfg.agent.done_gate else "stop is silent"))
    paused = pause_path(cfg).exists()
    checks.append(("not paused", not paused,
                   "`varyz hooks resume` to turn back on" if paused else "running"))
    folder = cfg.data_dir / "hooks"
    configs = client_configs if client_configs is not None else install.client_paths()
    # A client that is not installed has no hooks to check. Counting it held
    # every Claude-Code-only machine at NO-GO over Codex, and a machine with
    # neither -- Cursor only, or no agent yet -- at NO-GO over launchers that
    # nothing would ever run.
    hook_clients = [
        client for client in ("claude-code", install.CODEX_CLIENT)
        if client in configs and configs[client].exists()
    ]
    if os.name == "nt":
        from varyz.windows_hook import launcher_paths

        launchers = [path for path in launcher_paths(folder, install.HOOK_EVENTS).values()
                     if path.is_file() and Path(str(path) + ".launch").is_file()]
    else:
        launchers = sorted(folder.glob(f"{install.HOOK_MARKER}*.sh")) if folder.exists() else []
    if hook_clients:
        checks.append(("launchers", len(launchers) == len(install.HOOK_EVENTS),
                       f"{len(launchers)} of {len(install.HOOK_EVENTS)} in {folder}"))
    else:
        checks.append(("launchers", True, "not needed: neither Claude Code nor Codex is installed"))
    for client in hook_clients:
        settings_path = install.hook_settings_path(client, configs[client])
        installed = 0
        if settings_path is not None and settings_path.exists():
            with contextlib.suppress(OSError, ValueError):
                settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
                hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
                installed = sum(
                    any(install._is_varyz_group(group, folder.as_posix())
                        for group in hooks.get(event, []) or [])
                    for event in install.HOOK_EVENTS
                )
        checks.append((f"{client} hooks", installed == len(install.HOOK_EVENTS),
                       f"{installed} of {len(install.HOOK_EVENTS)} events"
                       + ("" if installed else f"; run `varyz connect {client}`")))
    checks.append(("hook errors", *_error_status(cfg)))
    from varyz.resolution.task_size import classifier_status

    if not quick:
        checks.append(("task classifier", *classifier_status(cfg)))
        checks.append(("rule reader", *_reader_status(cfg)))
    return checks


def _reader_status(cfg: Any) -> tuple[bool, str]:
    """Who reads the owner's turns, and whether a reading that is due has run.

    Informational: a reading waiting for the plan's limits is working as meant.
    """
    from varyz.resolution import headless_reader

    if not cfg.workflows.rule_reader_agent:
        return True, "off (`[workflows] rule_reader_agent`)"
    if cfg.workflows.reader_runner == "agent":
        return True, "asked of the working agent (`reader_runner = \"agent\"`)"
    config_file = cfg.source_file or _config_file()
    try:
        runners = headless_reader.find_runners(cfg, config_file) if config_file else {}
    except Exception as exc:  # informational: never what fails the doctor
        return True, f"cannot tell which sign-in runs it ({type(exc).__name__})"
    if not runners:
        return True, "no Claude Code or Codex sign-in to run it on; the agent is asked"
    return True, f"on {' or '.join(sorted(runners))}: {headless_reader.summary(cfg)}"


def _error_status(cfg: Any) -> tuple[bool, str]:
    """Only a failure the installed code could still make is a NO-GO.

    The log is append-only and nothing clears it, so one failure from a build
    since replaced -- a NameError from a half-saved edit -- held the doctor at
    NO-GO for good, and a reader could not tell it from a hook failing on every
    prompt. A failure counts when it is newer than the newest source file of
    this package; older lines are reported, not failed on.
    """
    errors = _errors_path(cfg)
    try:
        lines = [line for line in errors.read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        return True, "none"
    if not lines:
        return True, "none"
    installed = max(
        (path.stat().st_mtime for path in Path(__file__).parent.rglob("*.py")),
        default=0.0,
    )
    current: list[str] = []
    for line in lines:
        stamp, _, rest = line.partition(" ")
        try:
            when = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            # No time: written by a build from before lines carried one, which
            # this code has replaced by definition.
            continue
        if when > installed:
            current.append(rest)
    if current:
        return False, f"{len(current)} since this code was installed, last: {current[-1]}"
    return True, f"none since this code was installed; {len(lines)} older in {errors}"


def _errors_path(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "runtime" / "hooks" / "errors.log"


def _record_trial_uses(
    cfg: Any, briefing: dict[str, Any], session_id: str | None, task: str
) -> None:
    """Tell the daemon which rules on trial this task was shown, so one that held
    over enough tasks is confirmed. Best effort: the hook opened the archive
    read-only, and a briefing never waits on the daemon."""
    if not briefing.get("trial_rules"):
        return
    from varyz.resolution.briefing import shown_rule_ids
    from varyz.resolution.client import ResolutionClient, ResolutionClientError
    from varyz.resolution.trials import task_key

    resolution = cfg.resolution
    if not _port_open(resolution.bind_host, resolution.port):
        return
    client = ResolutionClient(
        resolution.bind_host, resolution.port, auth_mode=resolution.auth_mode,
        archive=cfg.db_path, timeout=2.0,
    )
    with contextlib.suppress(ResolutionClientError, OSError, ValueError):
        client.record_principle_uses(
            shown_rule_ids(briefing), task_key=task_key(session_id, task),
            device_id=resolution.device_id,
        )


def _daemon_check(cfg: Any) -> tuple[bool, str]:
    """Whether this archive's own daemon answers on its port."""
    from varyz.resolution.client import (
        ArchiveMismatchError,
        ResolutionClient,
        ResolutionClientError,
    )

    resolution = cfg.resolution
    where = f"{resolution.bind_host}:{resolution.port}"
    if not _port_open(resolution.bind_host, resolution.port):
        from varyz.resolution.availability import is_on_demand, is_paused

        if is_on_demand(cfg) and resolution.autostart and not is_paused(cfg):
            return True, f"{where}: on-demand standby; startup on first use is unverified"
        return False, f"{where}: not running; run `varyz daemon start`"
    client = ResolutionClient(
        resolution.bind_host,
        resolution.port,
        auth_mode=resolution.auth_mode,
        archive=cfg.db_path,
        timeout=2.0,
    )
    try:
        client.status()
    except ArchiveMismatchError as exc:
        return False, str(exc)
    except ResolutionClientError as exc:
        return False, f"{where}: {exc}"
    return True, where


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def run(event_name: str, config: Path | None) -> int:
    """Run one hook; always exit 0. Output only on success."""
    global _CONFIG_PATH
    _CONFIG_PATH = config
    try:
        event = _read_event()
        if os.environ.get(HEADLESS_ENV):
            # varyz's own reader: its client must not brief or gate it.
            return 0
        cfg = _load_cfg(config)
        if pause_path(cfg).exists():
            return 0
        notice = ""
        if event_name in {"session-start", "user-prompt", "stop"}:
            with contextlib.suppress(Exception):
                from varyz.doctor import session_notice

                notice = session_notice(cfg, str(event.get("session_id") or ""),
                                        config_path=_config_file())
        try:
            result = HANDLERS[event_name](cfg, event)
            session_id = str(event.get("session_id") or "")
            if session_id:
                with contextlib.suppress(OSError):
                    state_file = _state_path(cfg, session_id)
                    state = _load_state(state_file)
                    if state.pop("doctor_hook_failure", None):
                        _save_state(state_file, state)
        except Exception as exc:
            with contextlib.suppress(Exception):
                _log_failure(config, event_name, exc)
            result = None
            failure_seen = False
            session_id = str(event.get("session_id") or "")
            if session_id:
                with contextlib.suppress(OSError):
                    state_file = _state_path(cfg, session_id)
                    state = _load_state(state_file)
                    failure_seen = state.get("doctor_hook_failure") == event_name
                    state["doctor_hook_failure"] = event_name
                    _save_state(state_file, state)
            if not notice and not failure_seen:
                notice = (
                    "Varyz operational notice: tell the user once as a small note: "
                    "'Varyz could not complete its session hook. Run varyz doctor for details.' "
                    "Continue the task."
                )
        if notice:
            hook_event = "SessionStart" if event_name == "session-start" else "UserPromptSubmit"
            if event_name == "stop":
                result = result or {}
                result["systemMessage"] = "Varyz needs attention. Run varyz doctor for details."
            elif result is None:
                result = _context(hook_event, notice)
            elif "hookSpecificOutput" in result:
                output = result["hookSpecificOutput"]
                output["additionalContext"] = _joined(output.get("additionalContext", ""), notice)
        if result is not None:
            _emit(result)
    except Exception as exc:  # fail open: varyz must never break the session
        with contextlib.suppress(Exception):
            _log_failure(config, event_name, exc)
    return 0


def _log_failure(config: Path | None, event_name: str, exc: Exception) -> None:
    cfg = _load_cfg(config)
    log = _errors_path(cfg)
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    message = " ".join(str(exc).split())
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {event_name}: {type(exc).__name__}: {message}\n")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in HANDLERS:
        sys.stderr.write(f"usage: python -m varyz.hooks {{{'|'.join(HANDLERS)}}} [--config PATH]\n")
        return 0
    config: Path | None = None
    if "--config" in args:
        index = args.index("--config")
        if index + 1 < len(args):
            config = Path(args[index + 1])
    return run(args[0], config)


if __name__ == "__main__":
    raise SystemExit(main())
