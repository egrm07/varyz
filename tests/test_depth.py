from varyz.retrieval.depth import classify_depth


def test_classifies_simple_memory_lookup():
    profile = classify_depth("find the moment when I went kayaking")
    assert profile.level == 1
    assert profile.label == "simple"


def test_classifies_when_question_as_simple_lookup():
    profile = classify_depth("when did I sign the Berlin lease?")
    assert profile.level == 1


def test_classifies_reflective_pattern_question():
    profile = classify_depth("what patterns show up in how my relationship changed?")
    assert profile.level >= 3


def test_classifies_manipulation_question_as_sensitive_deep():
    profile = classify_depth("was I manipulated by them?")
    assert profile.level == 5
    assert profile.is_deep


def test_classifies_happiest_moment_as_deep_inference():
    profile = classify_depth("what was my happiest moment?")
    assert profile.level >= 4
    assert profile.is_deep


def test_classifies_best_friend_as_deep_inference():
    profile = classify_depth("who is my best friend?")
    assert profile.level >= 4
    assert profile.is_deep
