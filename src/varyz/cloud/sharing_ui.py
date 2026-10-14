"""Escaped, server-rendered sharing flows; no browser state grants authority."""

import html
import json
import time

from .sharing_service import identifier


def esc(value):
    return html.escape(str(value), quote=True)


def shell(title, content, *, project=False, nav=True):
    from .ui import workspace_nav

    return (
        "<style>.shared-shell{max-width:none;margin:auto}.shared-shell h1{font-size:42px}"
        ".shared-row{padding:24px 0;border-bottom:1px solid var(--edge)}"
        ".shared-shell pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        ".shared-shell input,.shared-shell textarea,.shared-shell button{"
        "min-height:44px;max-width:100%}"
        ".shared-shell nav{display:flex;gap:12px 24px;flex-wrap:wrap;margin-bottom:32px}"
        ".shared-shell nav a{flex-shrink:0;white-space:nowrap}"
        ".shared-shell fieldset{border:0;padding:0}.shared-shell input[type=checkbox]{width:22px}"
        ".shared-shell label{display:block;margin:12px 0}.shared-shell textarea{width:100%}"
        ".shared-shell label:has(input[type=checkbox]){display:flex;align-items:center;gap:12px}"
        ".shared-shell input[type=checkbox]{flex:0 0 22px;margin:0;min-height:22px}"
        ".shared-shell summary{min-height:44px;cursor:pointer}"
        ".shared-setup{border:1px solid var(--edge);padding:24px;margin:24px 0}"
        ".shared-action{display:inline-flex;align-items:center;min-height:44px;"
        "padding:10px 18px;background:var(--signal);color:var(--night);text-decoration:none}"
        ".shared-setup ol{padding-left:22px}.shared-copy-status{min-height:24px}"
        ".shared-setup textarea{min-height:100px;padding:12px}"
        ".shared-shell a{overflow-wrap:anywhere}"
        ".shared-shell .shared-row>a.shared-card-action{font:400 14px/1.4 var(--sans);"
        "display:inline-flex;align-items:center;min-height:44px;"
        "padding:8px 16px;border:1px solid var(--edge);border-radius:6px;"
        "text-decoration:none;font-size:14px;box-sizing:border-box}"
        ".shared-card-action:hover{border-color:var(--signal)}"
        ".shared-cancel{display:inline-flex;align-items:center;min-height:44px;margin-right:24px}"
        ".shared-project-page .cloud-dashboard-heading{margin:0 0 12px}"
        ".shared-project-page .cloud-dashboard-heading h1{margin:0}"
        ".shared-project-page .shared-back{margin-bottom:4px}"
        ".shared-project-page .shared-project-intro{padding-top:16px}"
        ".shared-sent-dialog{width:min(420px,calc(100vw - 64px));padding:28px;"
        "box-sizing:border-box;border:1px solid var(--edge);border-radius:10px;"
        "background:var(--night,#11120f);color:var(--ink);box-shadow:0 24px 80px #0008}"
        ".shared-sent-dialog::backdrop{background:#0009}"
        ".shared-sent-dialog h2{margin:0 0 12px}.shared-sent-dialog p{"
        "color:var(--muted);line-height:1.6;margin:0 0 24px;overflow-wrap:anywhere}"
        ".shared-sent-dialog form{margin:0;text-align:right}"
        ".shared-back{display:inline-block;margin:0 0 20px;font-size:13px;color:var(--muted)}"
        ".shared-project-intro{display:flex;align-items:center;justify-content:space-between;"
        "gap:24px;padding:24px 0 32px;border-bottom:1px solid var(--edge)}"
        ".shared-project-intro p{max-width:540px;color:var(--muted);margin:12px 0 0;"
        "line-height:1.6}"
        ".shared-project-intro .shared-action{flex-shrink:0;border-radius:5px;font-size:14px}"
        ".shared-status{font-size:12px;color:var(--muted);letter-spacing:.04em;"
        "text-transform:uppercase}"
        ".shared-people{margin:30px 0 40px}.shared-people h2{margin:0 0 20px}"
        ".shared-person{padding:18px 0;border-bottom:1px solid var(--edge);overflow-wrap:anywhere}"
        ".shared-person p{margin:0 0 10px}.shared-person strong{font-weight:500}"
        ".shared-list-label{font:400 12px var(--sans);text-transform:uppercase;"
        "letter-spacing:.08em;"
        "color:var(--muted);margin:28px 0 0}"
        ".shared-invite-row{display:flex;align-items:center;justify-content:space-between;gap:20px}"
        ".shared-person-state{display:block;color:var(--muted);font-size:13px;margin-top:6px}"
        ".shared-invite-row form{margin:0;flex-shrink:0}"
        ".shared-person button{background:transparent;color:var(--ink);"
        "border:1px solid var(--edge);"
        "border-radius:5px;padding:8px 14px}"
        ".shared-disclosure{border-top:1px solid var(--edge);padding:8px 0}"
        ".shared-disclosure>summary{display:flex;align-items:center;gap:12px;list-style:none;"
        "font-size:15px;padding:8px 0}"
        ".shared-disclosure>summary::-webkit-details-marker{display:none}"
        ".shared-disclosure>summary:after{content:'+';margin-left:auto;"
        "color:var(--muted);font-size:20px}"
        ".shared-disclosure[open]>summary:after{content:'−'}"
        ".shared-detail-count{color:var(--muted);font-size:12px}"
        ".shared-preferences>p{font-size:14px;line-height:1.6;max-width:650px}"
        ".shared-preferences article{border:0;border-left:2px solid var(--edge);border-radius:0;"
        "background:transparent;padding:8px 20px;margin:20px 0}"
        ".shared-update-note{color:var(--signal)}"
        "@media(max-width:600px){.shared-project-intro{align-items:flex-start;flex-direction:column;gap:20px}"
        ".shared-invite-row{align-items:flex-start;flex-direction:column;gap:14px}}"
        ".shared-shell h1{font-family:var(--serif);font-weight:400;margin-bottom:20px}"
        ".shared-shell h2{font:400 25px/1.3 var(--serif);margin:32px 0 18px}"
        ".shared-shell>p{max-width:650px;color:var(--muted);line-height:1.65}"
        ".shared-shell form{max-width:720px}.shared-shell fieldset{margin:24px 0}"
        ".shared-shell legend{font-size:18px;margin-bottom:12px}"
        ".shared-shell label:has(input[type=checkbox]),.shared-shell label:has(input[type=radio]){"
        "padding:16px;border:1px solid var(--edge);border-radius:6px;"
        "background:#191a16;line-height:1.6}"
        ".shared-shell label:has(input:checked){border-color:#76643b;background:#242218}"
        ".shared-shell label:has(input[type=radio]){display:flex;align-items:flex-start;gap:12px}"
        ".shared-shell input[type=radio]{min-height:22px;width:22px;flex:0 0 22px;margin:2px 0}"
        ".shared-flow{display:flex;gap:18px;color:var(--muted);font-size:12px;margin:24px 0}"
        ".shared-flow strong{color:var(--signal);font-weight:500}"
        ".shared-invitation{padding:8px 0 32px}"
        ".shared-invitation>input{width:100%;box-sizing:border-box}"
        ".shared-invitation select{min-height:48px;width:100%;padding:10px 14px;"
        "background:var(--night,#11120f);color:var(--ink,#fff);"
        "border:1px solid var(--edge);font:inherit}"
        ".shared-invitation p{color:var(--muted);font-size:14px;line-height:1.6;margin:12px 0 28px}"
        ".shared-invite-actions{display:flex;justify-content:flex-end;"
        "align-items:center;gap:24px;margin-top:36px}"
        ".shared-project-picker{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));"
        "gap:16px;margin:28px 0}"
        ".shared-project-choice{padding:24px;border:1px solid var(--edge);border-radius:6px;"
        "text-decoration:none;background:#191a16}"
        ".shared-project-choice strong{display:block;font:400 24px var(--serif);margin-bottom:12px}"
        ".shared-project-choice span{font-size:13px;color:var(--muted)}"
        ".shared-project-choice:hover{border-color:var(--signal)}"
        ":focus-visible{outline:2px solid var(--signal);outline-offset:4px}"
        "@media(max-width:500px){.shared-shell h1{font-size:34px}"
        ".shared-shell nav{gap:12px 18px}.shared-project-picker{grid-template-columns:1fr}}</style>"
        '<main class="shared-shell'
        + (" shared-project-page" if project else "")
        + '"><header class="cloud-dashboard-heading">'
        f'<h1 tabindex="-1">{esc(title)}</h1></header>'
        + (workspace_nav("questions" if title == "Questions" else "shared") if nav else "")
        + content
        + "</main>"
    )


def hidden(name, value):
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def form(action, csrf, fields, button="Continue", request_key=None):
    return (
        f'<form method="post" action="{esc(action)}">'
        + hidden("csrf", csrf)
        + hidden("idempotency_key", request_key or identifier())
        + fields
        + f"<button>{esc(button)}</button></form>"
    )


def items(values):
    content = ""
    for item in values:
        body = item["body"]
        if item["kind"] == "rule":
            text = "<p>" + esc(body["statement"]) + "</p>"
        elif item["kind"] == "answer":
            text = "<h3>" + esc(body["question"]) + "</h3><p>" + esc(body["answer"]) + "</p>"
        else:
            text = (
                "<h3>"
                + esc(body["name"])
                + "</h3><ol>"
                + "".join(
                    "<li>"
                    + esc(s["step"])
                    + (": " + esc(s["how"]) if s.get("how") else "")
                    + (" (optional)" if s.get("optional") else "")
                    + "</li>"
                    for s in body["steps"]
                )
                + "</ol>"
            )
        if item.get("origin") == "partner_decision":
            text += (
                "<p><small>Decided by " + esc(item.get("decided_by", "a partner")) + "</small></p>"
            )
        content += (
            '<article class="shared-row"><small>'
            + esc(item["scope_label"])
            + "</small>"
            + text
            + "</article>"
        )
    return content or "<p>No preferences are currently shared.</p>"


LEVELS = (
    ("consult", "Consult", "Their AI follows your shared preferences and asks you the rest."),
    (
        "contribute",
        "Contribute",
        "Also lets their own Varyz fill small routine gaps. You see each one and can overrule it.",
    ),
    (
        "decide",
        "Decide",
        "A partner: they can answer project questions, also while you are away. "
        "You can overrule any decision.",
    ),
)


def level_fields(level="consult", fill_gaps=True):
    fields = "<fieldset><legend>What they can do</legend>"
    for value, name, help_text in LEVELS:
        fields += (
            f'<label><input type="radio" name="level" value="{value}"'
            + (" checked" if value == level else "")
            + "><span><strong>"
            + name
            + "</strong>: "
            + esc(help_text)
            + "</span></label>"
        )
    return (
        fields
        + '<label><input type="checkbox" name="fill_gaps" value="yes"'
        + (" checked" if fill_gaps else "")
        + "> Their Varyz can fill routine gaps (Contribute and Decide)</label></fieldset>"
    )


def when(value):
    return time.strftime("%d %b %H:%M UTC", time.gmtime(value))


def report(value):
    lines = "<p>" + esc(value.get("summary", "")) + "</p>"
    if value.get("next_milestone"):
        lines += "<p>Next milestone: " + esc(value["next_milestone"]) + "</p>"
    code = [
        label + " <code>" + esc(value[key]) + "</code>"
        for key, label in (("branch", "Branch"), ("commit", "Commit"), ("run", "Run"))
        if value.get(key)
    ]
    if code:
        lines += "<p>" + " · ".join(code) + "</p>"
    for key, label in (
        ("decisions", "Reported decisions"),
        ("checks", "Reported checks"),
        ("open_questions", "Open questions"),
        ("open_work", "Still open"),
        ("next_steps", "Next steps"),
    ):
        if value.get(key):
            lines += (
                "<p>"
                + label
                + ":</p><ul>"
                + "".join("<li>" + esc(line) + "</li>" for line in value[key])
                + "</ul>"
            )
    return (
        '<article class="shared-row"><small>'
        + esc(value["by"])
        + " · "
        + when(value["at"])
        + ("" if not value.get("new") else " · new")
        + "</small>"
        + lines
        + "</article>"
    )


def project_overview(value):
    content = '<section class="shared-project-overview"><h2>About this project</h2>'
    if value["explanation"]:
        content += "<p>" + esc(value["explanation"]) + "</p>"
        content += (
            '<p class="cloud-muted">Reviewed by the owner · '
            + when(value["explanation_at"])
            + "</p>"
        )
    else:
        content += '<p class="cloud-muted">The owner has not shared a project explanation yet.</p>'
    latest = value["latest_work"]
    content += "<h2>Latest work</h2>"
    if latest:
        content += (
            '<p class="cloud-muted">Reported work; completion is not independently verified.</p>'
        )
        content += report(
            {
                "summary": latest["summary"],
                "by": latest["by"],
                "at": latest["at"],
                "checks": latest.get("checks", []),
            }
        )
    else:
        content += '<p class="cloud-muted">No work report shared yet.</p>'
    for key, label in (
        ("open_work", "Still open"),
        ("open_questions", "Open questions"),
        ("next_steps", "Next steps"),
    ):
        content += "<h3>" + label + "</h3>"
        if value[key]:
            content += (
                "<ul>" + "".join("<li>" + esc(line) + "</li>" for line in value[key]) + "</ul>"
            )
        else:
            content += '<p class="cloud-muted">Not reported yet.</p>'
    if value.get("next_milestone"):
        content += "<p>Next milestone: " + esc(value["next_milestone"]) + "</p>"
    return content + "</section>"


def context_editor(share, csrf, saved=None):
    from .sharing_service import encoded
    from .store import digest

    draft = share.get("context_draft")
    value = draft or share.get("context_published") or {"explanation": "", "story": []}
    content = '<details class="shared-disclosure"' + (" open" if draft else "") + ">"
    content += (
        "<summary>"
        + ("Review project explanation and background" if draft else "Edit project explanation")
        + "</summary>"
    )
    fields = hidden(
        "fingerprint", saved.get("fingerprint", "") if saved else digest(encoded(value))
    )
    fields += (
        '<label for="project-explanation">What is this project and what is it meant to do?</label>'
    )
    fields += '<textarea id="project-explanation" name="explanation" maxlength="1200" rows="4">'
    fields += esc(saved.get("explanation", "") if saved else value["explanation"]) + "</textarea>"
    fields += '<p class="cloud-muted">Save only a factual explanation you agree with. '
    fields += "</p>"
    if value["story"]:
        fields += '<h3>Background for collaborators’ AIs</h3><p class="cloud-muted">'
        fields += "Review what will be sent. It stays out of the recipient’s overview.</p>"
        for entry in value["story"]:
            fields += '<article class="shared-row"><p>' + esc(entry["text"]) + "</p><small>"
            fields += esc(entry["by"]) + " · " + esc(entry["at"]) + " · " + esc(entry["source"])
            fields += "</small></article>"
        checked = saved.get("include_story") == "yes" if saved else True
        fields += '<label><input type="checkbox" name="include_story" value="yes"'
        fields += (" checked" if checked else "") + "> "
        fields += "Include this background for their AI</label>"
    content += form(
        "/account/shares/" + share["id"] + "/context", csrf, fields, "Save shared context"
    )
    return content + "</details>"


def collaboration(share, csrf):
    digest = share["collaboration"]
    action = "/account/sharing/action"
    repo = digest["repo"]
    content = "<h2>Code</h2>"
    content += (
        "<p>Repository: <code>"
        + esc(repo["url"])
        + "</code>"
        + (" (" + esc(repo["branch"]) + ")" if repo.get("branch") else "")
        + "</p>"
        if repo
        else "<p>Link the repository so collaborators' AIs can get the code and try each "
        "other's branches. Their AIs set up access themselves.</p>"
    )
    content += (
        "<details><summary>Change repository</summary>"
        if repo
        else "<details><summary>Add a code repository (optional)</summary>"
    )
    content += form(
        action,
        csrf,
        hidden("operation", "link_repo")
        + hidden("share_id", share["id"])
        + '<label>Repository URL <input name="repo" maxlength="300" required '
        'placeholder="https://github.com/you/project" value="'
        + esc(repo["url"] if repo else "")
        + '"></label><label>Main branch (optional) <input name="branch" maxlength="120" value="'
        + esc(repo.get("branch", "") if repo else "")
        + '"></label>',
        "Change repository" if repo else "Link repository",
    )
    content += "</details>"
    if digest["access_requests"]:
        content += "<h2>Access requests</h2>"
    for request in digest["access_requests"]:
        content += (
            '<article class="shared-row"><p>'
            + esc(request["name"])
            + " asked for repository access"
            + (
                " as <code>" + esc(request["github_username"]) + "</code>"
                if request.get("github_username")
                else ""
            )
            + ". Your AI can add them when you say yes"
            + (
                ", or run:</p><pre>" + esc(request["command"]) + "</pre>"
                if request.get("command")
                else ".</p>"
            )
            + form(
                action,
                csrf,
                hidden("operation", "access_granted")
                + hidden("share_id", share["id"])
                + hidden("membership_id", request["membership_id"]),
                "I added them",
            )
            + "</article>"
        )
    if digest["review"]:
        content += "<h2>To review</h2>"
    for value in digest["review"]:
        fields = hidden("operation", "review") + hidden("share_id", share["id"])
        fields += hidden("review_id", value["review_id"])
        if value["kind"] == "disagreement":
            content += (
                '<article class="shared-row"><small>'
                + esc(value.get("by", "Collaborator"))
                + " followed your preference but suggests a change</small><p>"
                + esc(value.get("preference", ""))
                + "</p><p><strong>Suggestion:</strong> "
                + esc(value.get("suggestion", ""))
                + "</p>"
                + form(action, csrf, fields + hidden("decision", "dismiss"), "Keep mine")
                + form(action, csrf, fields + hidden("decision", "adopt"), "Adopt the suggestion")
                + "</article>"
            )
            continue
        source = (
            esc(value.get("by", "Partner"))
            + " decided"
            + (" while you were away" if value.get("while_away") else "")
            if value["kind"] == "partner_decision"
            else esc(value.get("by", "Collaborator")) + "'s Varyz filled a gap"
        )
        content += (
            '<article class="shared-row"><small>'
            + source
            + "</small><h3>"
            + esc(value.get("question", ""))
            + "</h3><p>"
            + esc(value.get("answer", ""))
            + "</p>"
            + form(action, csrf, fields + hidden("decision", "keep"), "Keep")
            + form(
                action,
                csrf,
                fields
                + hidden("decision", "overrule")
                + '<label>Your answer (optional) <textarea name="answer" maxlength="4000">'
                "</textarea></label>",
                "Overrule",
            )
            + "</article>"
        )
    if digest["reports"]:
        content += "<h2>Recent work</h2>" + "".join(report(value) for value in digest["reports"])
    return content


def invitation(project, csrf, draft=None, error=""):
    draft = draft or {}
    fields = hidden("project", project)
    if draft.get("share_id"):
        fields += hidden("share_id", draft["share_id"])
    fields += (
        '<label for="share-explanation">What is this project and what is it meant to do?</label>'
    )
    fields += '<textarea id="share-explanation" name="explanation" maxlength="1200" rows="3">'
    fields += esc(draft.get("explanation", "")) + "</textarea>"
    fields += '<p class="cloud-muted">A factual explanation for them and their AI. '
    fields += "Only describe an intended purpose you agree with. You can add this later.</p>"
    fields += (
        '<label for="share-recipient">Email or username</label>'
        '<input id="share-recipient" name="recipient" required maxlength="254" '
        'autocomplete="off" autocapitalize="none" spellcheck="false" '
        'aria-describedby="share-recipient-help" '
        'placeholder="name@example.com or @username" value="'
        + esc(draft.get("recipient", ""))
        + '">'
        '<p id="share-recipient-help" class="cloud-muted">'
        "Use a Varyz username, or invite anyone by email. Joining is free.</p>"
        '<label for="share-access">Access</label><select id="share-access" name="level">'
        + "".join(
            '<option value="'
            + level
            + '"'
            + (" selected" if draft.get("level", "consult") == level else "")
            + ">"
            + esc(label)
            + "</option>"
            for level, label in (
                ("consult", "Follow preferences"),
                ("contribute", "Contribute"),
                ("decide", "Decide"),
            )
        )
        + '</select><p class="cloud-muted">'
        "Follow preferences: their AI asks you when something is missing. "
        "Contribute: it can fill routine gaps. Decide: they can answer project questions.</p>"
        '<p class="cloud-muted">Shares this project’s preferences. '
        "General preferences and private history stay private.</p>"
        '<div class="shared-invite-actions"><a href="/account/shared">Cancel</a>'
        "<button>Send invitation</button></div>"
    )
    content = ('<p role="alert">' + esc(error) + "</p>") if error else ""
    content += (
        '<form class="shared-invitation" method="post" action="/account/shares/new">'
        + hidden("csrf", csrf)
        + hidden("idempotency_key", draft.get("idempotency_key") or identifier())
        + fields
        + "</form>"
    )
    return shell("Share " + project, content)


def invitation_sent(result):
    return (
        '<dialog class="shared-sent-dialog" aria-labelledby="shared-sent-title" '
        'aria-describedby="shared-sent-message" open>'
        '<h2 id="shared-sent-title">Invitation sent</h2><p id="shared-sent-message">'
        + esc(result.get("recipient", "They"))
        + " can join "
        + esc(result["label"])
        + ' from their invitation.</p><form method="dialog"><button autofocus>Done</button>'
        "</form></dialog>"
    )


def pending_action(draft, csrf):
    labels = {
        "invite": "Send invitation",
        "link_repo": "Link repository",
        "set_level": "Change access",
        "review": "Submit review",
        "access_granted": "Confirm repository access",
        "revoke_invite": "Revoke invitation",
        "retract_answer": "Remove shared answer",
        "leave": "Leave project",
        "revoke": "Remove access",
        "state": "Change project access",
        "withdraw": "Withdraw question",
    }
    operation = draft.get("operation", "")
    label = labels.get(operation)
    if not label:
        return shell(
            "Review sharing again",
            '<p>Nothing was changed.</p><p><a href="/account/shared">Back to Shared</a></p>',
        )
    fields = "".join(hidden(k, v) for k, v in draft.items() if k not in ("csrf", "idempotency_key"))
    summary = (
        "<p>Sign-in completed. Review this action before continuing. Nothing has changed yet.</p>"
    )
    for key, title in (
        ("invited_email", "Recipient"),
        ("level", "Access"),
        ("repo", "Repository"),
        ("branch", "Branch"),
        ("decision", "Decision"),
        ("answer", "Your answer"),
        ("status", "Project access"),
    ):
        if draft.get(key):
            summary += "<p>" + esc(title) + ": " + esc(draft[key]) + "</p>"
    return shell(
        label,
        summary
        + form(
            "/account/sharing/action",
            csrf,
            fields,
            label,
            draft.get("idempotency_key") or None,
        )
        + '<p><a href="/account/shared">Cancel</a></p>',
    )


def selection(records, project, csrf, share=None, draft=None, error="", next_after=None):
    draft = draft or {}
    share_id = share["id"] if share else None
    action = "/account/shares/" + share_id + "/preview" if share_id else "/account/shares/new"
    selected = set(json.loads(draft.get("selected", "[]")))
    groups = {project: "", "*": ""}
    visible = set()
    for record in records:
        scope = record["payload"]["scope"]
        if scope not in groups:
            continue
        identity = record["kind"] + ":" + record["id"] + ":" + str(record["version"])
        visible.add(identity)
        text = record["payload"].get(
            "statement", record["payload"].get("question", record["payload"].get("name", ""))
        )
        groups[scope] += (
            "<label>"
            + f'<input type="checkbox" name="selected" value="{esc(identity)}"'
            + (" checked" if identity in selected else "")
            + "><span>"
            + esc(text)
            + "</span></label>"
        )
    fields = hidden("project", project)
    fields += "".join(hidden("selected", identity) for identity in selected - visible)
    if share:
        fields += hidden("expected_publication_revision", share["revision"])
    fields += (
        ("<h2>Invite someone</h2>" if not share else "")
        + "<details><summary>Project name</summary><label>Collaboration name "
        '<input name="label" maxlength="80" required value="'
        + esc(draft.get("label", share["label"] if share else project))
        + '"></label></details>'
    )
    if not share:
        fields += (
            '<label>Recipient email <input type="email" name="invited_email" required value="'
            + esc(draft.get("invited_email", ""))
            + '"></label><p>We’ll email their invitation. Receiving a project is free.</p>'
        )
        fields += (
            "<details><summary>Permissions: follow preferences by default</summary>"
            + level_fields(draft.get("level", "consult"), draft.get("fill_gaps", "yes") == "yes")
            + "</details>"
        )
    fields += (
        ("<h2>Choose what to share</h2>" if not share else "")
        + "<fieldset><legend>Project preferences</legend>"
        + (groups[project] or "<p>No confirmed preferences on this page.</p>")
        + "</fieldset>"
    )
    fields += (
        "<details><summary>General preferences: choose individually</summary>"
        + groups["*"]
        + "</details>"
    )
    content = (
        '<div class="shared-flow" aria-label="Sharing steps"><strong>1 Choose</strong>'
        "<span>2 Review and send</span></div>"
        "<p>Only the preferences you select will be available. Private his"
        "tory and rationale stay private.</p>"
    )
    if error:
        content += '<p role="alert">' + esc(error) + "</p>"
    if next_after:
        fields += hidden("next_after", next_after)
        fields += (
            '<button type="submit" class="cloud-secondary" name="step" value="more" '
            "formnovalidate>More preferences, keep selection</button>"
        )
    return shell(
        "Choose what to share", content + form(action, csrf, fields, "Review selected preferences")
    )


def review(preview, csrf, email="", share_id=None, level="consult", fill_gaps="yes"):
    action = (
        "/account/shares/" + share_id + "/publish" if share_id else "/account/shares/new/publish"
    )
    fields = hidden("preview_id", preview["preview_id"]) + hidden(
        "source_fingerprint", preview["source_fingerprint"]
    )
    fields += hidden("invited_email", email)
    if not share_id:
        fields += hidden("level", level) + hidden("fill_gaps", fill_gaps)
    if share_id:
        fields += hidden("expected_publication_revision", preview["expected_publication_revision"])
    fields += (
        '<label><input type="checkbox" name="reviewed" value="yes" require'
        "d> I reviewed this exact shared text.</label>"
    )
    content = (
        "<p>This is the exact text available to the recipient’s AI. Privat"
        "e edits require another publication; retractions stop access imme"
        "diately.</p>"
    )
    if share_id:
        content += (
            "<details><summary>Current published version</summary>"
            + items(preview["diff"]["before"])
            + "</details>"
        )
    content += items(preview["items"]) + form(
        action, csrf, fields, "Publish update" if share_id else "Publish and create invitation"
    )
    return shell("Review " + preview["label"], content)


def copy_box(label, value, button, target, *, multiline=False):
    field = (
        '<textarea readonly id="' + esc(target) + '">' + esc(value) + "</textarea>"
        if multiline
        else '<input readonly id="' + esc(target) + '" value="' + esc(value) + '">'
    )
    return (
        '<label for="'
        + esc(target)
        + '">'
        + esc(label)
        + "</label>"
        + field
        + '<button type="button" data-copy-target="'
        + esc(target)
        + '">'
        + esc(button)
        + '</button><p class="shared-copy-status" id="'
        + esc(target)
        + '-status" role="status" aria-live="polite"></p>'
        "<noscript><p>Select the text above and copy it.</p></noscript>"
    )


def behavior():
    return """
const sentDialog = document.querySelector('.shared-sent-dialog');
if (sentDialog && typeof sentDialog.showModal === 'function') {
  sentDialog.removeAttribute('open');
  sentDialog.showModal();
  sentDialog.addEventListener('close', () => {
    sentDialog.remove();
    document.querySelector('.shared-project-intro .shared-action')?.focus({preventScroll:true});
  });
}
const sharedTabs = Array.from(document.querySelectorAll('.shared-section-switch [role=tab]'));
if (sharedTabs.length) {
  const selectSharedTab = (name, focus = false) => {
    const selected = sharedTabs.find(tab => tab.id === 'shared-tab-' + name);
    if (!selected) return;
    sharedTabs.forEach(tab => {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
    });
    if (focus) selected.focus({preventScroll: true});
  };
  const restoreSharedTab = () => {
    const hash = location.hash.slice(1);
    const query = new URL(location.href).searchParams;
    selectSharedTab(['by-me', 'with-me'].includes(hash) ? hash :
      query.get('tab') || (query.has('cursor') ? 'with-me' : 'by-me'));
  };
  const activateSharedTab = tab => {
    const url = new URL(tab.href);
    // Keep the incoming page when changing tabs, and avoid anchor scrolling.
    const cursor = new URL(location.href).searchParams.get('cursor');
    if (cursor) url.searchParams.set('cursor', cursor);
    history.pushState(null, '', url);
    selectSharedTab(url.hash.slice(1), true);
  };
  sharedTabs.forEach((tab, index) => {
    tab.addEventListener('click', event => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      activateSharedTab(tab);
    });
    tab.addEventListener('keydown', event => {
      const target = event.key === 'ArrowRight' ? (index + 1) % sharedTabs.length :
        event.key === 'ArrowLeft' ? (index + sharedTabs.length - 1) % sharedTabs.length :
        event.key === 'Home' ? 0 : event.key === 'End' ? sharedTabs.length - 1 : null;
      if (target === null) return;
      event.preventDefault();
      activateSharedTab(sharedTabs[target]);
    });
  });
  window.addEventListener('popstate', restoreSharedTab);
  window.addEventListener('hashchange', restoreSharedTab);
  restoreSharedTab();
}
document.querySelectorAll('[data-copy-target]').forEach(button => {
  button.addEventListener('click', async () => {
    const field = document.getElementById(button.dataset.copyTarget);
    const status = document.getElementById(button.dataset.copyTarget + '-status');
    try {
      if (!navigator.clipboard) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(field.value);
      status.textContent = 'Copied.';
    } catch (_) {
      field.focus(); field.select();
      status.textContent = 'The text is selected. Use your browser’s Copy command.';
    }
  });
});
const setup = document.querySelector('[data-setup-status]');
if (setup) {
  let checking = false;
  const check = async () => {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch(setup.dataset.setupStatus,
        {credentials:'same-origin', cache:'no-store'});
      if (!response.ok) return;
      const current = await response.json();
      if (current.stage !== setup.dataset.setupStage) window.location.reload();
    } catch (_) { /* Keep the project usable while offline. */ }
    finally { checking = false; }
  };
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);
  setInterval(check, 15000);
}
"""


def onboarding(share, owner, status, plugin_url, resource):
    stage = status["stage"]
    label = share["label"]
    prompt = (
        "Use the Varyz Shared project "
        + json.dumps(label, ensure_ascii=False)
        + ", shared by "
        + json.dumps(owner, ensure_ascii=False)
        + " for this task."
    )
    content = (
        '<section class="shared-setup" id="setup" aria-labelledby="setup-title" '
        'data-setup-stage="'
        + esc(stage)
        + '" data-setup-status="/api/sharing/v1/memberships/'
        + esc(share["membership_id"])
        + '/connection">'
        '<p>Invitation accepted</p><h2 id="setup-title">Project added</h2>'
        "<p>Paste this into your AI chat to start working together.</p>"
        + copy_box(
            "Request for your AI", prompt, "Copy request", "shared-project-request", multiline=True
        )
    )
    if stage == "ready":
        content += '<p role="status">Your AI has read the current shared preferences.</p>'
    elif stage == "verify":
        content += '<p role="status">Your AI connection is saved. No need to connect again.</p>'
    else:
        content += (
            "<details><summary>Connect an AI if needed</summary>"
            "<p>Already use Varyz Shared? Use your existing connection. "
            "Otherwise, connect it once with the account that accepted this invitation.</p>"
        )
        if plugin_url:
            content += (
                '<a class="shared-action" href="'
                + esc(plugin_url)
                + '" target="_blank" rel="noopener noreferrer">Connect ChatGPT</a>'
                "<p>Choose Connect and allow access, then paste the request into your chat.</p>"
            )
        content += "</details>"
    content += (
        "<details><summary>Use another AI tool</summary>"
        "<p>Add this server to an app with remote MCP support and sign in.</p>"
        + copy_box("Server address", resource, "Copy server address", "shared-server")
        + "</details>"
    )
    content += (
        '<noscript><p><a href="/account/shared/'
        + esc(share["membership_id"])
        + '#setup">Refresh connection status</a></p></noscript></section>'
    )
    return content


def created(result):
    return shell(
        "Invitation sent",
        "<p>Varyz emails them and shows a desktop notification. They join from their "
        "agent or with Join in Shared → With me.</p>"
        + copy_box("Invitation link", result["invite_url"], "Copy invitation", "shared-invitation")
        + '<p>The link is a fallback; it works only for their account.</p><a href="/account/shares/'
        + esc(result.get("share_id", ""))
        + '">Manage shared project</a>',
    )


def reply_form(thread, csrf, draft=None, error=""):
    draft = draft or {}
    content = (
        "<p>Requested by "
        + esc(thread["guest"])
        + " for "
        + esc(thread["label"])
        + "</p><h2>"
        + esc(thread["question"])
        + "</h2>"
    )
    if thread.get("reason"):
        content += "<p>Collaborator’s explanation: " + esc(thread["reason"]) + "</p>"
    if error:
        content += (
            '<p role="alert">Your reply was not sent. Your draft is still here. '
            + esc(error)
            + "</p>"
        )
    fields = hidden("expected_thread_version", thread["version"]) + hidden(
        "publication_revision", thread["publication_revision"]
    )
    fields += (
        '<label>Your reply <textarea name="answer" maxlength="4000" required>'
        + esc(draft.get("answer", ""))
        + "</textarea></label>"
    )
    for key, label in [
        ("remember_project", "Remember for this project"),
        ("publish_to_share", "Add to shared preferences"),
    ]:
        fields += (
            '<label><input type="checkbox" name="'
            + key
            + '" value="1"'
            + (" checked" if draft.get(key) else "")
            + "> "
            + label
            + "</label>"
        )
    content += "<p>Unchecked: this reply goes only to this requester.</p>" + form(
        "/account/questions/" + thread["id"] + "/reply",
        csrf,
        fields,
        "Send reply",
        draft.get("idempotency_key"),
    )
    content += (
        "<details><summary>Decline this question</summary>"
        + form(
            "/account/questions/" + thread["id"] + "/decline",
            csrf,
            hidden("expected_thread_version", thread["version"])
            + (
                '<label>Explanation <textarea name="explanation" maxlength="4000">'
                + esc(draft.get("explanation", ""))
                + "</textarea></label>"
            ),
            "Decline",
            draft.get("idempotency_key") if draft.get("explanation") else None,
        )
        + "</details>"
    )
    return shell("Reply to collaborator", content)


# Public Varyz: a creator's published standards, followed by username.

PUBLIC_STYLE = (
    "<style>.public-kicker{font:400 12px var(--sans);text-transform:uppercase;"
    "letter-spacing:.08em;color:var(--muted);margin:0 0 8px}"
    ".public-meta{color:var(--muted);font-size:14px;margin:0 0 24px}"
    ".public-follow{display:flex;align-items:center;flex-wrap:wrap;gap:16px 24px;"
    "padding:0 0 32px;border-bottom:1px solid var(--edge)}"
    ".public-follow form{margin:0}.public-follow p{margin:0;color:var(--muted);"
    "font-size:14px;line-height:1.6;max-width:460px}"
    ".public-follow button,.public-primary{background:var(--signal);color:var(--night);border:0;"
    "border-radius:5px;padding:10px 20px;font:500 15px var(--sans);min-height:44px;"
    "cursor:pointer;text-decoration:none;display:inline-flex;align-items:center}"
    ".public-follow .public-following{background:transparent;color:var(--ink);"
    "border:1px solid var(--edge)}"
    ".public-topic{padding:8px 0 16px;border-bottom:1px solid var(--edge)}"
    ".public-topic h2{margin:32px 0 4px!important}"
    ".public-topic .cloud-count{margin:0 0 8px}"
    ".public-standards article{border-left:2px solid var(--edge);padding:4px 0 4px 20px;"
    "margin:18px 0}.public-standards article p{margin:0;line-height:1.65}"
    ".public-standards h3{font:500 16px/1.5 var(--sans);margin:0 0 6px}"
    ".public-standards ol{margin:6px 0 0;padding-left:20px;line-height:1.65}"
    ".public-topics .shared-row{display:flex;align-items:center;justify-content:space-between;"
    "flex-wrap:wrap;gap:12px 20px}.public-topics .shared-row form{margin:0}"
    ".public-topics .shared-row strong{font:400 22px var(--serif)}"
    ".public-topics .cloud-count{margin:4px 0 0}"
    ".public-row-actions{display:flex;gap:12px;align-items:center}"
    ".public-row-actions button,.public-row-actions a{background:transparent;"
    "color:var(--ink);border:1px solid var(--edge);border-radius:5px;padding:8px 14px;"
    "font:400 14px var(--sans);min-height:44px;box-sizing:border-box;margin:0;"
    "display:inline-flex;align-items:center;text-decoration:none}"
    ".public-row-actions button:hover,.public-row-actions a:hover{background:transparent;"
    "border-color:var(--muted)}"
    ".public-topic-form{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}"
    ".public-topic-form label{flex:1 1 260px;margin:0}"
    ".public-topic-form input[name=name],.public-topic-form input[name=username]{"
    "width:100%;box-sizing:border-box}"
    ".public-notice{border-left:2px solid var(--signal);padding:4px 0 4px 18px;margin:0 0 28px}"
    ".public-notice p{margin:0;color:var(--ink)}"
    ".public-pick{margin:18px 0 0}.public-pick fieldset{margin:12px 0}"
    ".public-pick input[type=radio]{accent-color:var(--signal)}"
    ".public-pick .cloud-count{margin:0 0 8px}"
    "@media(max-width:600px){.public-topic-form button{width:100%}"
    "#public-post{min-height:170px}}</style>"
)


def standard(item):
    body = item["body"]
    if item["kind"] == "rule":
        return "<article><p>" + esc(body["statement"]) + "</p></article>"
    if item["kind"] == "answer":
        return (
            "<article><h3>" + esc(body["question"]) + "</h3><p>"
            + esc(body["answer"]) + "</p></article>"
        )
    return (
        "<article><h3>"
        + esc(body["name"])
        + "</h3><ol>"
        + "".join(
            "<li>"
            + esc(s["step"])
            + (": " + esc(s["how"]) if s.get("how") else "")
            + (" (optional)" if s.get("optional") else "")
            + "</li>"
            for s in body.get("steps", [])
        )
        + "</ol></article>"
    )


def counted(n, one, many):
    return str(n) + " " + (one if n == 1 else many)


def profile(value, csrf, signed_in, notice=None):
    """The page anyone opens from a creator's post: their standards and one Follow button."""
    name = value["username"]
    content = PUBLIC_STYLE
    if notice:
        content += '<div class="public-notice" role="status"><p>' + esc(notice) + "</p></div>"
    content += '<p class="public-kicker">Public Varyz</p>'
    content += (
        "<p>The standards @" + esc(name) + " actually applies, published so your AI can "
        "use them too.</p>"
    )
    meta = [counted(value["followers"], "follower", "followers")]
    if value["updated"] and value["topics"]:
        meta.append("updated " + time.strftime("%d %b %Y", time.gmtime(value["updated"])))
    content += '<p class="public-meta">' + esc(" · ".join(meta)) + "</p>"
    content += '<div class="public-follow">'
    if value["own"]:
        content += (
            '<a class="public-primary" href="/account/public">Edit what is public</a>'
            "<p>This is your public Varyz. Only what you published appears here.</p>"
        )
    elif value["following"]:
        content += (
            '<form method="post" action="/@' + esc(name) + '/unfollow">'
            + hidden("csrf", csrf)
            + '<button class="public-following">Following · Unfollow</button></form>'
            "<p>Your AI uses @" + esc(name) + "’s standards on matching tasks. Your own "
            "Varyz still wins where you differ.</p>"
        )
    else:
        content += (
            '<form method="post" action="/@' + esc(name) + '/follow">'
            + hidden("csrf", csrf)
            + "<button>Follow @" + esc(name) + "</button></form><p>"
            + (
                "Free. "
                if signed_in
                else "Free with a Varyz account: sign in once and you are following. "
            )
            + "Your AI uses these standards on matching tasks, and your own Varyz still "
            "wins where you differ.</p>"
        )
    content += "</div>"
    if value["paused"]:
        content += (
            '<div class="shared-empty" style="margin-top:28px"><strong>Paused</strong><p>@'
            + esc(name) + "’s standards are paused for now. Followers keep their follow "
            "and get them again when they return.</p></div>"
        )
    elif not value["topics"]:
        content += (
            '<div class="shared-empty" style="margin-top:28px"><strong>Nothing published '
            "yet</strong><p>@" + esc(name) + " has not published standards. Follow now "
            "and they reach your AI once published.</p></div>"
        )
    for topic in value["topics"]:
        content += (
            '<section class="public-topic"><h2>' + esc(topic["title"]) + "</h2>"
            '<p class="cloud-count">'
            + counted(len(topic["items"]), "standard", "standards")
            + '</p><div class="public-standards">'
            + "".join(standard(item) for item in topic["items"])
            + "</div></section>"
        )
    content += (
        '<details class="shared-disclosure" style="margin-top:28px"><summary>How following '
        "works</summary><ol>"
        "<li>Follow by username. Your AI gets these standards whenever a task is about one "
        "of the topics.</li>"
        "<li>Your own Varyz rules and answers always win.</li>"
        "<li>When two people you follow cover the same topic, the one you followed most "
        "recently answers until you pick one. Naming @" + esc(name) + " in a task uses "
        "them for it.</li>"
        "<li>Unfollow any time. Nothing of yours is shared with @" + esc(name) + ".</li>"
        "</ol></details>"
    )
    return shell("@" + name, content, nav=signed_in)


def public_locked(message):
    """Publishing is a Cloud plan feature; say so plainly and keep following free."""
    content = PUBLIC_STYLE + (
        "<p>Publish the standards you actually apply, one topic at a time, so anyone can "
        "follow you by username and their AI uses them on matching tasks.</p>"
        '<p role="status">' + esc(message) + "</p>"
        '<p><a class="public-primary" href="/?view=dashboard&amp;section=account">Open '
        "Account</a></p>"
        '<p class="cloud-muted">Following others is free. Find them in '
        '<a href="/account/shared?tab=with-me#following">Shared, With me</a>.</p>'
    )
    return shell("Public Varyz", content)


def public_home(value, suggestions, csrf, notice=None):
    """The creator's Public Varyz page: link, topics, reach and a post to share."""
    content = PUBLIC_STYLE
    if notice:
        content += '<div class="public-notice" role="status"><p>' + esc(notice) + "</p></div>"
    if not value["published"]:
        content += (
            "<p>Publish the standards you actually apply, one topic at a time. Anyone can "
            "follow @" + esc(value["username"]) + " and their AI uses your standards on "
            "matching tasks. Your history, project preferences and reasons stay private.</p>"
        )
    else:
        content += (
            '<p>Anyone can follow you at <a href="' + esc(value["url"]) + '">'
            + esc(value["url"].split("://", 1)[-1]) + "</a>.</p>"
        )
        uses = value["uses_this_week"]
        content += (
            '<p class="public-meta">'
            + esc(
                counted(value["followers"], "follower", "followers")
                + " · used in "
                + counted(uses["tasks"], "task", "tasks")
                + " and "
                + counted(uses["answers"], "answer", "answers")
                + " this week"
            )
            + "</p>"
        )
        if not value["serving"]:
            content += (
                '<div class="shared-empty"><strong>Paused</strong><p>Followers are not '
                "getting your standards while your Cloud plan is inactive. Renew it in "
                "Account and they resume.</p></div>"
            )
        if value["post"]:
            content += '<div class="shared-setup">' + copy_box(
                "A post to share", value["post"], "Copy post", "public-post", multiline=True
            ) + "</div>"
        content += '<h2>Topics</h2><div class="public-topics">'
        for topic in value["topics"]:
            content += (
                '<div class="shared-row"><div><strong>' + esc(topic["topic"]) + "</strong>"
                '<p class="cloud-count">'
                + counted(topic["standards"], "standard", "standards")
                + '</p></div><div class="public-row-actions">'
                '<a href="/account/public/topic?name=' + esc(topic["slug"]) + '">Edit</a>'
                '<form method="post" action="/account/public/remove">'
                + hidden("csrf", csrf) + hidden("topic", topic["slug"])
                + "<button>Remove</button></form></div></div>"
            )
        content += "</div>"
    content += "<h2>" + ("Add a topic" if value["published"] else "Choose a topic") + "</h2>"
    if suggestions:
        content += '<div class="shared-project-picker">' + "".join(
            '<a class="shared-project-choice" href="/account/public/topic?name='
            + esc(s["slug"]) + '"><strong>' + esc(s["title"]) + "</strong><span>"
            + counted(s["count"], "preference fits", "preferences fit")
            + " · choose →</span></a>"
            for s in suggestions
        ) + "</div>"
    content += (
        '<form class="public-topic-form" method="get" action="/account/public/topic">'
        "<label>" + ("Or name a topic" if suggestions else "Topic")
        + ' <input name="name" required maxlength="40" '
        'placeholder="Writing, design, testing"></label>'
        "<button>Choose standards</button></form>"
    )
    if value["published"]:
        content += (
            '<details class="shared-disclosure" style="margin-top:36px"><summary>Turn off '
            "Public Varyz</summary><p>Followers stop getting your standards at once and "
            "your page shows nothing. They keep following, so publishing again reaches "
            'them.</p><form method="post" action="/account/public/off">'
            + hidden("csrf", csrf)
            + '<button class="cloud-secondary">Turn off</button></form></details>'
        )
    return shell("Public Varyz", content)


PUBLIC_CHOOSE_BATCH = 12


def public_choose(value, csrf, error="", selected=None, visible_count=PUBLIC_CHOOSE_BATCH):
    """Step one: tick the General preferences that belong in this topic."""
    chosen = (
        selected
        if selected is not None
        else {
            item["kind"] + ":" + item["id"] + ":" + str(item["version"])
            for item in value["items"]
            if item["suggested"]
        }
    )
    title = "Choose " + value["title"].lower() + " standards"
    visible_count = min(len(value["items"]), max(PUBLIC_CHOOSE_BATCH, visible_count))
    content = PUBLIC_STYLE + (
        "<style>.public-choose-form{padding-bottom:100px}"
        ".public-choose-actions{position:fixed;bottom:0;left:0;right:0;z-index:20;"
        "background:var(--night,#11120f);border-top:1px solid var(--edge);"
        "padding:16px 24px calc(16px + env(safe-area-inset-bottom,0px))}"
        ".public-choose-actions>div{max-width:988px;margin:auto;display:flex;"
        "align-items:center;justify-content:space-between;gap:20px}"
        ".public-choose-actions p{margin:0;color:var(--muted);font-size:13px}"
        ".public-choose-actions p span{display:block}"
        ".public-choose-actions button{flex-shrink:0}"
        "#standards-more{scroll-margin-top:24px}"
        "@media(max-width:500px){.public-choose-actions{padding-left:20px;"
        "padding-right:20px}.public-choose-actions>div{gap:12px}}</style>"
        '<div class="shared-flow" aria-label="Publishing steps"><strong>1 Choose</strong>'
        "<span>2 Review and publish</span></div>"
        "<p>Tick the General preferences that belong in "
        + esc(value["title"].lower())
        + ". Ones that look like a fit are ticked for you; answers about you never are. "
        "Project preferences stay private.</p>"
    )
    if error:
        content += '<p role="alert">' + esc(error) + "</p>"
    if not value["items"]:
        content += (
            '<div class="shared-empty"><strong>No General preferences yet</strong><p>Confirm '
            "rules that apply to all your work in Varyz first, then come back.</p></div>"
        )
        return shell(title, content)
    boxes = ""
    batch_start = ((visible_count - 1) // PUBLIC_CHOOSE_BATCH) * PUBLIC_CHOOSE_BATCH
    for index, item in enumerate(value["items"]):
        identity = item["kind"] + ":" + item["id"] + ":" + str(item["version"])
        if index >= visible_count:
            # Keep suggested/published choices intact until the owner changes them.
            if identity in chosen:
                boxes += hidden("selected", identity)
            continue
        body = item["body"]
        if item["kind"] == "answer":
            question = body["question"].strip()
            text = question + ("" if question[-1:] in "?.!:" else ":") + " " + body["answer"]
        elif item["kind"] == "workflow":
            text = body.get("name", "") + " (" + counted(
                len(body.get("steps") or []), "step", "steps") + ")"
        else:
            text = body.get("statement") or body.get("name", "")
        boxes += (
            '<label' + (' id="standards-more"' if index == batch_start else '')
            + '><input type="checkbox" name="selected" value="' + esc(identity) + '"'
            + (" checked" if identity in chosen else "")
            + "><span>" + esc(text) + "</span></label>"
        )
    fields = hidden("topic", value["topic"]) + hidden("visible_count", visible_count) + (
        "<fieldset><legend>" + esc(value["title"]) + " standards</legend>" + boxes
        + "</fieldset>"
    )
    if visible_count < len(value["items"]):
        fields += (
            '<button type="submit" name="intent" value="load-more" '
            'formaction="/account/public/preview#standards-more" class="cloud-secondary">'
            'Load more</button>'
        )
    fields += (
        '<div class="public-choose-actions"><div><p>'
        + str(visible_count) + " of " + counted(len(value["items"]), "standard", "standards")
        + ' shown<span>Next: review your selection</span></p><button type="submit">Finish</button>'
        '</div></div>'
    )
    content += (
        '<form class="public-choose-form" method="post" action="/account/public/preview">'
        + hidden("csrf", csrf) + hidden("idempotency_key", identifier()) + fields + '</form>'
    )
    return shell(title, content)


def public_review(preview, csrf, url):
    """Step two: the exact public text, one explicit confirmation, then publish."""
    content = PUBLIC_STYLE + (
        '<div class="shared-flow" aria-label="Publishing steps"><span>1 Choose</span>'
        "<strong>2 Review and publish</strong></div>"
        "<p>This exact text goes on " + esc(url.split("://", 1)[-1]) + " under "
        + esc(preview["title"]) + ". Anyone can read it and their AI can use it. Editing a "
        "preference later changes nothing here until you publish again; deleting it or "
        "making it project-only takes it down.</p>"
    )
    if preview["replaces"]:
        content += (
            "<p>This replaces the " + counted(preview["replaces"], "standard", "standards")
            + " published under " + esc(preview["title"]) + " now.</p>"
        )
    content += '<div class="public-standards">' + "".join(
        standard(item) for item in preview["items"]
    ) + "</div>"
    fields = hidden("preview_id", preview["preview_id"]) + (
        '<label><input type="checkbox" name="reviewed" value="yes" required> I reviewed '
        "this exact text. Anyone can read it.</label>"
    )
    content += form("/account/public/publish", csrf, fields, "Publish") + (
        '<p class="cloud-count" style="margin-top:20px"><a href="/account/public/topic?name='
        + esc(preview["topic"]) + '">Change selection</a></p>'
    )
    return shell("Review " + preview["title"].lower() + " standards", content)


def public_card(value):
    """The By me tab's Public Varyz row."""
    if value is None:
        return ""
    if value["published"]:
        line = (
            ", ".join(t["topic"] for t in value["topics"])
            + " · "
            + counted(value["followers"], "follower", "followers")
            + ("" if value["serving"] else " · paused")
        )
        action = "Manage"
    else:
        line = "Publish the standards you actually apply so anyone can follow you by username."
        action = "Publish standards"
    return (
        '<div class="shared-list-grid" style="margin-bottom:18px"><article class="shared-row">'
        '<a href="/account/public">Public Varyz</a><p class="cloud-muted">' + esc(line)
        + '</p><a class="shared-card-action" href="/account/public">' + action
        + "</a></article></div>"
    )


def following_section(data, csrf):
    """The With me tab's Following list: who, which topics, clashes to pick, unfollow."""
    content = PUBLIC_STYLE + (
        '<div class="shared-section-heading" id="following" style="margin-top:36px"><div>'
        '<h2>Following</h2><p class="cloud-count">Public standards your AI uses on '
        "matching tasks · free</p></div></div>"
    )
    rows = ""
    for creator in data["following"]:
        name = creator["creator"].lstrip("@")
        rows += (
            '<article class="shared-row"><a href="/@' + esc(name) + '">@' + esc(name) + "</a>"
            '<p class="cloud-muted">'
            + esc(
                (", ".join(creator["topics"]) or "Nothing published yet")
                + (" · paused" if creator.get("paused") else "")
            )
            + '</p><form method="post" action="/@' + esc(name) + '/unfollow">'
            + hidden("csrf", csrf) + hidden("back", "shared")
            + '<button class="cloud-secondary">Unfollow</button></form></article>'
        )
    if rows:
        content += '<div class="shared-list-grid">' + rows + "</div>"
    for choice in data["choices"]:
        options = "".join(
            '<label><input type="radio" name="username" value="' + esc(c.lstrip("@")) + '"'
            + (" checked" if c == choice["using"] else "")
            + "><span>" + esc(c) + "</span></label>"
            for c in choice["creators"]
        )
        content += (
            '<form class="public-pick" method="post" action="/account/following/pick">'
            + hidden("csrf", csrf) + hidden("topic", choice["slug"])
            + "<fieldset><legend>Who answers " + esc(choice["topic"].lower()) + "?</legend>"
            + '<p class="cloud-count">'
            + esc(
                "You picked " + choice["using"] + "."
                if choice.get("picked")
                else choice["using"] + " answers now because you followed them last."
            )
            + "</p>" + options + "</fieldset><button>Use for " + esc(choice["topic"].lower())
            + "</button></form>"
        )
    content += (
        '<form class="public-topic-form" method="post" action="/account/following" '
        'style="margin-top:20px">' + hidden("csrf", csrf)
        + '<label>Follow someone by username <input name="username" required maxlength="31" '
        'placeholder="@username" autocomplete="off"></label><button>Follow</button></form>'
    )
    return content
