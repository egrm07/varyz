from scripts.score_varyz_use_case_agent import score


def test_agent_scorer_requires_all_expected_references():
    cases = [
        {
            "id": "case",
            "use_case": "decision archaeology",
            "expect_groups": [["reason"], ["implementation"]],
            "required_refs": ["a", "b"],
        }
    ]
    payload = {
        "answers": [
            {
                "id": "case",
                "answer": "The reason and implementation.",
                "citations": ["a"],
                "abstained": False,
                "withheld_sensitive": False,
            }
        ]
    }
    assert score(cases, payload)["passed"] == 0


def test_agent_scorer_distinguishes_unknown_and_sensitive_abstentions():
    cases = [
        {"id": "unknown", "use_case": "unknown", "expect_none": True},
        {"id": "private", "use_case": "private", "expect_withheld": True},
    ]
    payload = {
        "answers": [
            {
                "id": "unknown",
                "answer": "",
                "citations": [],
                "abstained": True,
                "withheld_sensitive": False,
            },
            {
                "id": "private",
                "answer": "",
                "citations": [],
                "abstained": True,
                "withheld_sensitive": True,
            },
        ]
    }
    assert score(cases, payload)["passed"] == 2
