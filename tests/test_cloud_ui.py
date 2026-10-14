"""Dashboard privacy, readable controls and semantic preference form conversion."""

import html
import re
from html.parser import HTMLParser

import pytest

from varyz.cloud.payloads import canonical_payload
from varyz.cloud.ui import dashboard_style, form_scope, parse_preference_form, render_dashboard

OWNER = {"email": "owner@example.test", "status": "active", "subscription": "sub_test"}


def test_computer_shortcut_is_paid_and_keeps_setup_and_upload_choices_separate():
    text = render_dashboard(OWNER, [], "csrf")
    assert 'id="connect-computer"' in text
    assert 'id="computer-status"' in text and 'aria-live="polite"' in text
    assert "Connecting does not select or upload preferences" in text
    assert '<details id="computer-fallback">' in text
    assert 'id="computer-project-step"' in text and 'id="computer-review-step"' in text
    assert 'id="computer-all-projects"' in text
    assert "Select projects with clear evidence" in text
    assert "Approve and finish" in text and "Review what will sync" in text
    assert 'id="computer-done-step"' in text
    assert 'id="connect-computer"' not in render_dashboard(
        {**OWNER, "status": "inactive"},
        [],
        "csrf",
    )
    other_host = render_dashboard(OWNER, [], "csrf", public_url="https://other.example")
    assert 'id="connect-computer"' not in other_host
    assert 'id="cloud-setup-request"' in other_host


def test_paid_connection_guide_distinguishes_local_and_hosted_connections():
    rules = [record("rule", {"statement": "Use plain words", "scope": "*"})]
    hosted = render_dashboard(OWNER, rules, "csrf", connections=[{"client_id": "hosted-ai"}])
    assert "Waiting for your computer" in hosted
    assert "Waiting for your first sync" in hosted
    local = render_dashboard(OWNER, [], "csrf", connections=[{"client_id": "varyz-local"}])
    assert "Computer connected" in local and "Waiting for your first sync" in local
    synced = render_dashboard(OWNER, rules, "csrf", connections=[{"client_id": "varyz-local"}])
    assert "Preferences received" in synced
    assert "Connect another AI account or computer" in synced
    assert "fresh task" in synced and "Setup complete" not in synced


def test_setup_hides_dashboard_until_local_connection_and_received_preferences():
    rules = [record("rule", {"statement": "Use plain words", "scope": "*"})]
    for connections, preferences in [
        ([], rules),
        ([{"client_id": "hosted-ai"}], rules),
    ]:
        text = render_dashboard(OWNER, preferences, "csrf", connections=connections)
        assert 'class="cloud-connected-guide"' not in text
        assert 'id="cloud-setup-request"' in text
        assert 'id="cloud-dashboard-content" hidden' in text
        assert 'id="cloud-setup-content" class="cloud-setup-shell" data-cloud-entry="auto">' in text
    synced = render_dashboard(OWNER, rules, "csrf", connections=[{"client_id": "varyz-local"}])
    assert 'id="cloud-dashboard-content" hidden' not in synced
    assert (
        'id="cloud-setup-content" class="cloud-setup-shell" data-cloud-entry="auto" hidden'
        in synced
    )
    assert 'id="cloud-setup-request"' in synced
    assert 'data-cloud-view="projects"' in synced and 'id="preferences"' in synced


def test_explicit_setup_and_dashboard_views_keep_empty_setup_reachable():
    text = render_dashboard(OWNER, [], "csrf", setup_mode=False)
    assert 'id="cloud-dashboard-content" hidden' not in text
    assert 'href="/?view=setup#connect-varyz"' in text
    text = render_dashboard(
        OWNER,
        [record("rule", {"statement": "Plain words", "scope": "*"})],
        "csrf",
        connections=[{"client_id": "varyz-local"}],
        setup_mode=True,
    )
    assert 'id="cloud-dashboard-content" hidden' in text
    assert 'id="computer-done-title">Cloud setup complete' in text
    assert 'href="/?view=dashboard#preferences"' in text


def test_entry_uses_neutral_wording_and_dashboard_skip_does_not_approve_sync():
    text = render_dashboard(OWNER, [], "csrf")
    assert '<h1>Set up Cloud</h1>' not in text
    assert 'id="connect-title">Set up Cloud' not in text
    assert 'class="cloud-setup-skip" href="/?view=dashboard">Skip to dashboard</a>' in text
    assert 'data-cloud-entry="auto"' in text
    assert 'data-cloud-entry="auto"' not in render_dashboard(OWNER, [], "csrf", setup_mode=True)
    assert 'class="cloud-setup-skip"' not in render_dashboard(
        {**OWNER, "status": "inactive"}, [], "csrf"
    )


def test_payment_return_waits_for_entitlement_and_guide_preserves_upload_consent():
    unpaid = {**OWNER, "status": "inactive"}
    assert "cloud-setup-request" not in render_dashboard(unpaid, [], "csrf")
    waiting = render_dashboard(unpaid, [], "csrf", payment_return=True)
    assert "Waiting for payment confirmation" in waiting
    assert "cloud-setup-request" not in waiting
    text = render_dashboard(OWNER, [], "csrf", public_url="https://approved.example")
    assert "https://approved.example" in text
    assert "explicit upload approval" in text and "deletion counts" in text
    assert "publisher-supplied release" in text


def record(kind, payload):
    return {"kind": kind, "id": "record_1", "version": 2, "payload": payload}


class Controls(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.fields = []
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        values = dict(attrs)
        if "name" in values:
            self.fields.append(values)


def test_preferences_are_grouped_by_scope_in_closed_boxes():
    from xml.etree import ElementTree

    records = [
        record("rule", {"statement": "Shop rule", "scope": "shop"}),
        record("answer", {"question": "Tone?", "answer": "Calm", "scope": "*"}),
        record("rule", {"statement": "Another shop rule", "scope": "shop"}),
        record("rule", {"statement": "Project rule", "scope": "<Alpha>"}),
        {"kind": "rule", "id": "removed", "payload": None},
    ]
    text = render_dashboard({**OWNER, "status": "inactive"}, records, "csrf", setup_mode=False)
    fragment = re.search(r'<div class="cloud-preference-groups">(.*?)</section>', text, re.S)[0]
    fragment = fragment.removesuffix("</section>")
    root = ElementTree.fromstring(fragment)
    groups = root.findall("details")
    assert len(groups) == 3
    assert all("open" not in group.attrib for group in groups)
    assert [group.find("summary/span/span").text for group in groups] == [
        "General",
        "shop",
        "<Alpha>",
    ]
    assert [len(group.findall("div/article")) for group in groups] == [1, 2, 1]
    assert [
        group.find("summary/span/span[@class='cloud-group-count']").text for group in groups
    ] == [
        "1 preference",
        "2 preferences",
        "1 preference",
    ]
    assert "Applies to all projects" in text
    assert "removed" not in fragment


def test_dashboard_shows_semantic_cards_and_controls_without_json_or_credentials():
    records = [
        record("rule", {"statement": "Use calm prose", "scope": "*"}),
        record("answer", {"question": "Which tone?", "answer": "Warm", "scope": "shop"}),
        record(
            "workflow",
            {
                "name": "Ship",
                "scope": "shop",
                "steps": [
                    {"step": "Review", "how": "Read the preview", "optional": True},
                    {"step": "Publish"},
                ],
            },
        ),
    ]
    owner = {**OWNER, "access_token": "never-show-bearer", "password": "never-show-password"}
    text = render_dashboard(owner, records, "form-csrf")
    controls = Controls(text)
    assert "Use calm prose" in text and "Which tone?" in text
    assert "Read the preview" in text and "(optional)" in text
    assert "All projects" in text and "Cloud is active" in text
    assert "pre" not in controls.tags
    assert not any(field["name"] == "payload" for field in controls.fields)
    assert "never-show-bearer" not in text and "never-show-password" not in text
    assert {"statement", "question", "answer", "step_0", "how_0", "optional_0"} <= {
        field["name"] for field in controls.fields
    }
    assert not any(tag in controls.tags for tag in ("script", "iframe", "link"))


def test_dashboard_escapes_owner_records_question_connections_and_price_labels():
    attack = '<script>alert("x")</script>'
    text = render_dashboard(
        {**OWNER, "status": "canceled", "email": attack},
        [record("rule", {"statement": attack, "scope": attack})],
        'csrf"value',
        questions=[{"id": attack, "question": attack, "project": "shop"}],
        connections=[{"client": attack, "label": attack}],
        prices=[{"id": 'price"attack', "label": attack}],
    )
    assert "<script>" not in text
    assert html.escape(attack) in text
    assert 'value="csrf&quot;value"' in text
    assert 'value="price&quot;attack"' in text


def test_paid_and_unpaid_controls_and_supplied_prices_are_honest():
    text = render_dashboard(
        {**OWNER, "status": "inactive", "subscription": None},
        [],
        "csrf",
        prices=[{"id": "monthly", "label": "Cloud — €7 / month"}],
    )
    assert "Cloud — €7 / month" in text
    assert "$5" not in text and "$48" not in text
    assert "No free trial" not in text
    assert 'name="action" value="checkout"' in text
    assert 'name="action" value="cancel_subscription"' not in text
    active = render_dashboard(OWNER, [], "csrf")
    assert 'name="action" value="checkout"' not in active
    assert 'name="action" value="cancel_subscription"' in active


def test_every_plan_is_offered_and_subscription_dates_are_shown():
    text = render_dashboard(
        {**OWNER, "status": "inactive", "subscription": None},
        [],
        "csrf",
        prices=[
            {"id": "monthly", "label": "Subscribe, €7 a month", "plus_tax": True},
            {"id": "annual", "label": "Subscribe, €70 a year", "plus_tax": True},
        ],
    )
    assert 'value="monthly"' in text and 'value="annual"' in text
    assert "€70 a year" in text and "added at checkout" in text
    renewing = render_dashboard(OWNER, [], "csrf", billing={"renews": "7 November 2026"})
    assert "Renews 7 November 2026" in renewing
    assert 'name="action" value="cancel_subscription"' in renewing
    ending = render_dashboard(OWNER, [], "csrf", billing={"ends": "7 November 2026"})
    assert "until 7 November 2026 and will not renew" in ending
    assert 'name="action" value="cancel_subscription"' not in ending


def test_destructive_actions_require_confirmation_controls_and_pending_answers_are_semantic():
    text = render_dashboard(
        OWNER,
        [],
        "csrf",
        questions=[{"id": "pending", "question": "Which colour?", "project": "shop"}],
        connections=[{"client": "varyz-local"}],
    )
    controls = Controls(text)
    assert 'name="action" value="answer_question"' in text
    assert 'name="question_id" value="pending"' in text
    assert "Choose projects" in text
    assert "Type DELETE to confirm" in text
    assert all("required" in f for f in controls.fields if f["name"] == "confirm_action")
    assert {"export", "logout", "revoke_all", "revoke_connection", "delete_account"} <= {
        f.get("value") for f in controls.fields if f["name"] == "action"
    }


def test_responsive_layout_and_keyboard_access_have_no_external_assets():
    css = dashboard_style()
    assert "@media(max-width:760px)" in css
    assert "grid-template-columns:minmax(0,1fr)" in css
    assert ":focus-visible" in css
    assert all(int(size) >= 44 for size in re.findall(r"min-height:(\d+)px", css) if int(size) > 32)
    assert "overflow-wrap:anywhere" in css
    urls = re.findall(r"url\(['\"]?([^'\"\)]+)", css)
    assert urls and all(url.startswith("/fonts/") for url in urls)
    assert "@import" not in css


def test_form_rule_preserves_existing_optional_metadata_not_submitted():
    previous = {
        "statement": "Old",
        "kind": "vision",
        "scope": "shop",
        "domain": "website",
        "rationale": "Existing reason",
    }
    result = parse_preference_form(
        {"kind": "rule", "id": "rule-1", "base_version": "4", "statement": "New"}, previous
    )
    assert result == {
        "kind": "rule",
        "id": "rule-1",
        "base_version": 4,
        "payload": {**previous, "statement": "New"},
    }


def test_workflow_form_retains_order_guidance_and_explicit_optional_choices():
    data = {
        "kind": "workflow",
        "id": "workflow-1",
        "base_version": "1",
        "name": "Release",
        "scope_mode": "project",
        "scope_project": "shop",
        "step_0": "Review",
        "how_0": "Read preview",
        "optional_0": "1",
        "step_1": "Publish",
        "how_1": "",
        "step_2": "",
        "how_2": "",
    }
    previous = {
        "name": "Release",
        "scope": "shop",
        "domain": "website",
        "rationale": "Care",
        "steps": [{"step": "Old", "optional": True}, {"step": "Publish", "optional": True}],
    }
    payload = parse_preference_form(data, previous)["payload"]
    assert payload["steps"] == [
        {"step": "Review", "how": "Read preview", "optional": True},
        {"step": "Publish"},
    ]
    assert payload["domain"] == "website" and payload["rationale"] == "Care"
    assert canonical_payload("workflow", payload) == payload


@pytest.mark.parametrize(
    "data, message",
    [
        ({"kind": "unsupported"}, "supported"),
        ({"kind": "rule", "id": "bad/id", "base_version": "1"}, "identity"),
        ({"kind": "rule", "id": "ok", "base_version": "wrong"}, "version"),
        ({"kind": "workflow", "id": "ok", "base_version": "1", "step_20": "Too many"}, "20"),
        ({"kind": "workflow", "id": "ok", "base_version": "1", "how_0": "No step"}, "name"),
    ],
)
def test_form_rejects_invalid_wire_identifiers_and_step_controls(data, message):
    with pytest.raises(ValueError, match=message):
        parse_preference_form(data)


def test_scope_form_requires_explicit_project_name_and_supports_all_projects():
    assert form_scope({"scope_mode": "all", "scope_project": "ignored"}) == "*"
    assert form_scope({"scope_mode": "project", "scope_project": " shop "}) == "shop"
    with pytest.raises(ValueError, match="project name"):
        form_scope({"scope_mode": "project", "scope_project": " "})
