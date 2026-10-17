import json

from varyz.cloud.rule_briefing import briefing
from varyz.resolution.rule_selection import names_other_project, same_rule


def rule(identity, statement, scope="*", domain=""):
    return dict(
        id=identity, kind="rule", payload=dict(statement=statement, scope=scope, domain=domain)
    )


def test_conditions_and_word_order_never_collapsed():
    assert same_rule("Don't publish before review.", "Do not publish before review")
    assert not same_rule("Publish before review", "Review before publish")
    assert not same_rule(
        "Always run every check before publishing the finished product",
        "Always run every check before publishing the finished product unless urgent",
    )


def test_generic_project_names_and_cross_project_rules_remain():
    assert not names_other_project("Record every demo", "varyz", "fix checkpoints", ["demo"])
    assert not names_other_project("For SAN7 and Varyz run tests", "varyz", "fix", ["san7"])
    assert names_other_project("When launching SAN7 use Expo", "varyz", "fix", ["san7"])
    assert not names_other_project("When launching SAN7 use Expo", "varyz", "fix SAN7", ["san7"])


def test_large_briefing_pages_keep_whole_rules():
    records = [
        rule(str(i), f"Unique requirement {i}: " + chr(65 + i % 26) * 300) for i in range(175)
    ]
    records += [
        rule("other", "Use Expo", "san7"),
        rule("website", "Validate sitemap", domain="website"),
    ]
    first = briefing(records, "Make launch video footage", "varyz")
    assert len(json.dumps(first)) < len(json.dumps(records))
    assert first["more_rules_in_scope"] > 0
    ids = set()
    for page in range(1, 20):
        value = briefing(records, "Make launch video footage", "varyz", page=page)
        for item in value["rules"]:
            assert item["id"] not in ids
            ids.add(item["id"])
            assert len(item["statement"]) > 300
        if not value["more_rules_in_scope"]:
            break
    assert ids == {str(i) for i in range(175)}
    assert not first["definition_of_done"]
