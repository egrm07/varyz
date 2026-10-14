"""The reply schema written as a grammar, with nowhere to put whitespace."""

from __future__ import annotations

import json
import re

import pytest
from pydantic import BaseModel, ConfigDict, Field

from varyz.llm.grammar import gbnf_from_schema
from varyz.personal_model.annotation_wire import group_response_model
from varyz.personal_model.semantic_runtime import (
    SemanticCandidateResponse,
    _compact_schema,
)
from varyz.personal_model.synthesis import (
    AlternativeResponse,
    EvidenceSearchResponse,
    HypothesisProposalResponse,
)


def _rules(grammar: str) -> dict[str, str]:
    rules = {}
    for line in grammar.splitlines():
        if not line.strip():
            continue
        name, _, body = line.partition(" ::= ")
        rules[name.strip()] = body.strip()
    return rules


class _Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str = Field(alias="q")


class _Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(alias="n")
    spans: tuple[_Inner, ...] = Field(alias="e", min_length=1, max_length=3)
    note: str | None = Field(alias="s")


def test_the_grammar_leaves_no_room_for_whitespace():
    """Half a measured reply was spaces, and nothing reads them.

    The reply is parsed by :mod:`json`, which does not care, and it is never
    shown to anyone. Each space still costs a forward pass, so the saving is
    not in asking for fewer of them but in making them unwritable.
    """

    grammar = gbnf_from_schema(_Outer.model_json_schema())
    assert grammar is not None

    # Whitespace can only be generated where a rule contains it literally, and
    # the only rule that may is the one for the characters of a quotation.
    literal = re.compile(r'"((?:[^"\\]|\\.)*)"')
    for name, body in _rules(grammar).items():
        if name in {"char", "string"}:
            continue
        for text in literal.findall(body):
            assert not re.search(r"\s", text), f"{name} admits whitespace: {body}"


def test_the_grammar_pins_every_property_in_order():
    grammar = _rules(gbnf_from_schema(_Outer.model_json_schema()) or "")

    assert grammar["root"] == '"{" "\\"n\\":" integer ",\\"e\\":" root-e ",\\"s\\":" root-s "}"'
    assert grammar["root-s"] == "string | null"


def test_an_array_carries_its_own_bounds():
    grammar = _rules(gbnf_from_schema(_Outer.model_json_schema()) or "")

    # One to three, written as the first plus up to two more.
    assert grammar["root-e"] == '"[" inner ("," inner){0,2} "]"'


def test_every_bound_the_schema_states_is_a_bound_the_sampler_cannot_leave():
    """A bound the grammar drops costs the whole reply, not the one field.

    The reply is validated as one document. A confidence of 7, a message number
    of 0 or an empty quotation is legal in an unbounded grammar and refused by
    the schema, and the refusal throws away all eight episodes in the group.
    """

    class _Bounded(BaseModel):
        model_config = ConfigDict(extra="forbid")

        target: int = Field(alias="t", ge=1)
        offset: int = Field(alias="o", ge=0)
        confidence: float = Field(alias="c", ge=0, le=1)
        quote: str = Field(alias="q", min_length=1)

    rules = _rules(gbnf_from_schema(_Bounded.model_json_schema()) or "")

    assert rules["root"] == (
        '"{" "\\"t\\":" positive ",\\"o\\":" natural '
        '",\\"c\\":" unit ",\\"q\\":" nonempty-string "}"'
    )
    assert rules["positive"] == "[1-9] [0-9]*"
    assert rules["unit"] == '"0" ("." [0-9]+)? | "1" ("." "0"+)?'
    assert rules["nonempty-string"] == '"\\"" char+ "\\""'


def test_a_bound_that_cannot_be_written_exactly_is_refused():
    """Approximating one is the failure this exists to prevent, in reverse."""

    class _Awkward(BaseModel):
        score: int = Field(ge=3, le=97)

    assert gbnf_from_schema(_Awkward.model_json_schema()) is None


def test_an_optional_property_is_written_last_and_may_be_left_out():
    """Every subset the schema allows stays writable, in one fixed order.

    Refusing these was costing the two largest synthesis calls their grammar
    altogether, and with it the whitespace saving this module exists for. The
    order is a narrowing -- ``{"note":..., "name":...}`` cannot be written --
    but not of the replies allowed, only of their spelling, and the grammar is
    what the model decodes with, so it cannot want the other one.
    """

    class _Loose(BaseModel):
        name: str
        note: str | None = None

    rules = _rules(gbnf_from_schema(_Loose.model_json_schema()))

    assert rules["root"] == '"{" "\\"name\\":" string (",\\"note\\":" root-note)? "}"'


def test_an_object_with_nothing_required_is_still_refused():
    """With no fixed first member the comma has no fixed home."""

    class _Empty(BaseModel):
        note: str | None = None

    assert gbnf_from_schema(_Empty.model_json_schema()) is None


def test_a_length_bounded_string_is_counted_rather_than_approximated():
    class _Bounded(BaseModel):
        code: str = Field(min_length=2, max_length=8)

    rules = _rules(gbnf_from_schema(_Bounded.model_json_schema()))

    assert rules["string-2-8"] == '"\\"" char{2,8} "\\""'


def test_a_pattern_it_knows_becomes_a_character_class_of_its_own():
    """``^[^\\s]+$`` is every character that is not whitespace.

    Inside a JSON string the control range already excludes tab, newline,
    carriage return, form feed and vertical tab, so the space is the only one
    the class has to name, and the escapes kept are exactly those that spell a
    non-whitespace character.
    """

    class _Identified(BaseModel):
        stable_id: str = Field(min_length=1, max_length=512, pattern=r"^[^\s]+$")

    rules = _rules(gbnf_from_schema(_Identified.model_json_schema()))

    assert rules["string-1-512"] == '"\\"" non-space-char{1,512} "\\""'
    # The space is written as an escape rather than as itself: a literal space
    # inside a character class is legible to the parser but not to a reader.
    assert rules["non-space-char"] == '[^"\\\\\\x7F\\x00-\\x1F\\x20] | "\\\\" ["\\\\/]'
    # The whitespace escapes are the point: each spells a character the
    # pattern forbids, and a grammar that allowed them would be wider than
    # the schema by exactly the characters the schema exists to exclude.
    for escape in ("b", "f", "n", "r", "t", "u"):
        assert escape not in rules["non-space-char"].partition("|")[2]


def test_a_fixed_length_tuple_is_written_member_by_member():
    """A pair of ids is a pair, not a list that happens to be two long."""

    pair = {
        "type": "array",
        "prefixItems": [{"type": "string"}, {"type": "integer"}],
        "minItems": 2,
        "maxItems": 2,
    }
    rules = _rules(gbnf_from_schema({"type": "object", "properties": {"at": pair},
                                     "required": ["at"]}))

    assert rules["root-at"] == '"[" string "," integer "]"'


def test_a_tuple_that_may_be_short_or_long_is_refused():
    prefix = [{"type": "string"}, {"type": "string"}]
    assert gbnf_from_schema(
        {"type": "object", "required": ["at"], "properties": {
            "at": {"type": "array", "prefixItems": prefix, "minItems": 1, "maxItems": 2}}}
    ) is None
    assert gbnf_from_schema(
        {"type": "object", "required": ["at"], "properties": {
            "at": {"type": "array", "prefixItems": prefix, "minItems": 2, "maxItems": 2,
                   "items": {"type": "string"}}}}
    ) is None


def test_a_structural_pattern_becomes_rules_rather_than_a_character_class():
    """A key is words joined by single separators, which no character class says."""

    class _Keyed(BaseModel):
        canonical_value_key: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

    rules = _rules(gbnf_from_schema(_Keyed.model_json_schema()))

    assert rules["slug-word"] == "[a-z0-9]+"
    assert rules["slug"] == "slug-word (slug-sep slug-word)*"
    assert rules["slug-string"] == '"\\"" slug "\\""'
    # Three literals, not `[._-]`: a hyphen inside a character class reads as a
    # range in some grammars and as itself in others.
    assert rules["slug-sep"] == '"." | "_" | "-"'


def test_a_structural_pattern_beside_a_length_bound_is_refused():
    """The two would have to be enforced together, and this cannot count."""

    class _Both(BaseModel):
        key: str = Field(max_length=64, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

    assert gbnf_from_schema(_Both.model_json_schema()) is None


def test_a_pattern_rule_does_not_quietly_take_a_name_already_in_use():
    """Its rules refer to each other by name, so a renamed one would dangle."""

    schema = {
        "type": "object",
        "required": ["a", "b"],
        "properties": {"a": {"$ref": "#/$defs/slug"}, "b": {"$ref": "#/$defs/key"}},
        "$defs": {
            "slug": {"type": "integer", "minimum": 1},
            "key": {"type": "string", "pattern": r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"},
        },
    }

    assert gbnf_from_schema(schema) is None


def test_a_pattern_it_does_not_know_is_refused():
    class _Odd(BaseModel):
        code: str = Field(min_length=1, max_length=8, pattern=r"^[A-Z]{3}-\d+$")

    assert gbnf_from_schema(_Odd.model_json_schema()) is None


def test_a_shape_it_does_not_understand_is_refused():
    assert gbnf_from_schema({"type": "object", "additionalProperties": True}) is None
    assert gbnf_from_schema({"type": "array"}) is None
    assert gbnf_from_schema({"$ref": "#/$defs/absent"}) is None
    assert gbnf_from_schema({"enum": [1, 2]}) is None


def _reachable(grammar: str) -> dict[str, str]:
    """The rules a reply can actually be written with, from ``root`` down.

    A grammar carries every rule the converter defined, including the shared
    primitives no property happens to use.  What a decode can spend tokens
    inside is only what ``root`` reaches, so that is what gets counted.
    """

    rules = _rules(grammar)
    seen: set[str] = set()
    pending = ["root"]
    while pending:
        name = pending.pop()
        if name in seen or name not in rules:
            continue
        seen.add(name)
        # Literals are stripped first: a quoted property name is text, not a
        # reference, and `"note"` would otherwise read as a rule called note.
        body = re.sub(r'"(?:[^"\\]|\\.)*"', " ", rules[name])
        pending.extend(re.findall(r"[a-zA-Z][\w-]*", body))
    return {name: rules[name] for name in seen}


#: A digit run has no ceiling either, but a number cannot be spent the way a
#: string can: every one of these closes on a token the sampler is pushed
#: toward by the prompt, and none of them has ever been seen to run.  What ran
#: was a quotation and a list of ids, which is what this counts.
_COUNTED_AS_A_RUN = ("integer", "number", "natural", "positive", "unit")


def _unbounded(grammar: str) -> set[str]:
    """The reachable rules that can repeat something without a ceiling."""

    found = set()
    for name, body in _reachable(grammar).items():
        if name in _COUNTED_AS_A_RUN:
            continue
        stripped = re.sub(r'"(?:[^"\\]|\\.)*"', " ", body)
        for unit, _ in re.findall(r"(\S+)\s*(\*|\+|\{\d+,\})", stripped):
            if unit not in _COUNTED_AS_A_RUN and not unit.startswith("["):
                found.add(name)
    return found


@pytest.mark.parametrize(
    ("call", "model", "unbounded"),
    [
        # The two calls that lost every concept of the 2026-09-06 run. Five of
        # twelve produced exactly 8,192 tokens and stopped, behind prompts of
        # 6,100 to 6,900, because three fields carried a minimum length and no
        # maximum -- and a schema field with no maximum is a grammar rule a
        # greedy decode is free to keep filling. Nothing they reach may repeat
        # without a ceiling now, and that is the whole of the fix stated as a
        # property rather than as three field bounds.
        ("evidence", EvidenceSearchResponse, set()),
        ("alternative", AlternativeResponse, set()),
        # The three that still can, named rather than excused. `string` is
        # reached in a proposal only by `InterpretationScope.valid_from` and
        # `valid_to`, which pydantic renders as `format: date-time` and this
        # converter renders as a string with no length -- twenty-four of them
        # in a twelve-proposal reply, on a stage that has since run 120 windows
        # of 120 without a truncation. The candidate window reaches all three
        # and has a truncation history: three of the first nine windows of the
        # 2026-08-31 run came back as invalid JSON at a 4,096-token cap. Both
        # want the ceilings measured against what their caches already hold,
        # which is a stage-by-stage job and not this test's.
        ("proposal", HypothesisProposalResponse, {"string"}),
        (
            "candidate window",
            SemanticCandidateResponse,
            {"string", "nonempty-string", "slug"},
        ),
        # The quotation an annotation carries, which is the field the whole
        # cache is made of: 65,437 replies already hold one, so its ceiling
        # cannot be chosen without measuring them first.
        ("annotation group of 8", group_response_model(8), {"string", "nonempty-string"}),
    ],
)
def test_a_reply_the_compiler_generates_says_which_of_its_rules_may_run(
    call, model, unbounded
):
    """A ceiling the grammar does not carry is one the sampler need not keep.

    The schema still refuses an over-long reply, but by then the tokens are
    spent and the whole document is thrown away -- which is how twelve of
    twelve concepts came back unbuilt. This is the standing inventory: the
    two repaired calls carry no unbounded repetition at all, and the two that
    still do say exactly which rule, so a new one fails here rather than in
    an overnight run.
    """

    grammar = gbnf_from_schema(_compact_schema(model))
    assert grammar is not None, f"{call} lost its grammar"

    assert _unbounded(grammar) == unbounded


def test_the_annotation_reply_converts_and_offers_no_way_to_decline():
    """The shape this exists for: what the annotator asks the server to write."""

    grammar = gbnf_from_schema(_compact_schema(group_response_model(8)))
    assert grammar is not None
    rules = _rules(grammar)

    # Exactly eight answers, because eighteen calls in 122 returned one.
    assert rules["root-r"] == '"[" wireepisodeanswer ("," wireepisodeanswer){7} "]"'
    assert '"unknown"' not in grammar


@pytest.mark.parametrize(
    "value",
    [
        {"n": 1, "e": [{"q": "one"}], "s": None},
        {"n": -20, "e": [{"q": "a"}, {"q": "b"}, {"q": "c"}], "s": "note"},
    ],
)
def test_what_the_grammar_describes_is_what_the_model_reads_back(value):
    """The grammar and the schema have to agree, or a legal reply fails to parse."""

    grammar = gbnf_from_schema(_Outer.model_json_schema())
    assert grammar is not None
    compact = json.dumps(value, separators=(",", ":"))

    assert " " not in compact.replace('"note"', "")
    assert _Outer.model_validate_json(compact).count == value["n"]
