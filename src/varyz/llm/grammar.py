"""The reply schema as a grammar, with nowhere to put whitespace.

llama.cpp does not constrain a reply with a JSON schema directly.  It converts
the schema into a GBNF grammar and constrains the sampler with that, and the
grammar it writes admits optional whitespace between every token, because that
is what JSON allows.  The model then uses it.  Measured on a live reply, 17.8%
of the characters generated were spaces — indentation, and a space after every
colon and comma — and a space costs a forward pass exactly as a word does.

Nothing reads that whitespace.  The reply is parsed by :mod:`json`, which does
not care, and it is never shown to anyone.  So the grammar is written here
instead, from the same schema, with the whitespace rules simply absent: not
discouraged in a prompt, which the sampler would ignore, but unrepresentable.

This is a narrow converter, and deliberately so.  It understands the shapes the
annotation wire actually uses and refuses everything else by returning ``None``,
which leaves the caller on llama.cpp's own conversion.  A grammar that is subtly
wider than its schema would let a reply through that the domain then rejects; a
grammar that is subtly narrower would make a correct reply unwritable, and the
model would fill the context trying.  Refusing is the only safe third answer.
"""

from __future__ import annotations

import re
from typing import Any

# From llama.cpp's own grammars/json.gbnf, which is where the string rule has to
# agree exactly: a reply is only useful if :mod:`json` can read it back.
_COMMON = (
    'char ::= [^"\\\\\\x7F\\x00-\\x1F] | "\\\\" (["\\\\bfnrt/] | "u" [0-9a-fA-F]{4})\n'
    'string ::= "\\"" char* "\\""\n'
    'integer ::= "-"? ("0" | [1-9] [0-9]*)\n'
    'number ::= integer ("." [0-9]+)? ([eE] [-+]? [0-9]+)?\n'
    'boolean ::= "true" | "false"\n'
    'null ::= "null"\n'
)
# A bound the grammar does not carry is a reply the sampler is free to write and
# the schema then refuses — and a refusal costs the whole group of eight, not the
# one field.  These are the bounded shapes the wire uses, written so the sampler
# cannot leave them.
_BOUNDED = (
    'positive ::= [1-9] [0-9]*\n'
    'natural ::= "0" | [1-9] [0-9]*\n'
    'unit ::= "0" ("." [0-9]+)? | "1" ("." "0"+)?\n'
    'nonempty-string ::= "\\"" char+ "\\""\n'
)
# One entry per pattern this understands, and it understands very few by
# design.  A regex engine is not wanted here; what is wanted is that a pattern
# either has an exact GBNF equivalent or is refused like any other bound.
#
# ``^[^\s]+$`` is "one or more characters, none of them whitespace".  Inside a
# JSON string the excluded control range already covers tab, newline, carriage
# return, form feed and vertical tab, so the literal space is the only one left
# to exclude from the class.  Of the escapes, the three kept -- \" \\ \/ -- each
# stand for a non-whitespace character; the five dropped (\b \f \n \r \t) each
# stand for whitespace, and \uXXXX is dropped because it can spell one.  No
# value becomes unwritable by dropping them: JSON may carry any non-control
# character literally, so every string the schema admits still has a spelling
# the grammar admits.
_PATTERN_CHARS = {
    r"^[^\s]+$": (
        "non-space-char",
        '[^"\\\\\\x7F\\x00-\\x1F\\x20] | "\\\\" ["\\\\/]',
    ),
}
# Patterns whose shape is structural rather than per-character, so they cannot
# be composed with a length bound: the rule says what the string is, not what
# each of its characters may be.  A schema that carries one of these *and* a
# length bound is refused, because the two would have to be enforced together
# and this cannot count characters across an alternation.
#
# ``^[a-z0-9]+(?:[._-][a-z0-9]+)*$`` is a key: runs of lowercase alphanumerics
# joined by single dots, underscores or hyphens, never leading, trailing or
# doubled.  The separators are written as three literals rather than as a
# character class, because a hyphen inside a class is a range in some readings
# and a literal in others, and this is not the place to find out which.
_PATTERN_STRINGS = {
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$": (
        "slug",
        {
            "slug-word": "[a-z0-9]+",
            "slug-sep": '"." | "_" | "-"',
            "slug": "slug-word (slug-sep slug-word)*",
        },
    ),
}
_PRIMITIVE = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
}
_NOT_A_NAME = re.compile(r"[^a-zA-Z0-9-]+")


class _Unsupported(Exception):
    """A schema shape this converter will not guess at."""


def _literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sanitise(hint: str) -> str:
    return _NOT_A_NAME.sub("-", hint).strip("-").lower() or "x"


class _Grammar:
    def __init__(self, defs: dict[str, Any]) -> None:
        self._defs = defs
        self._rules: dict[str, str] = {}
        self._named_defs: dict[str, str] = {}

    def _define(self, hint: str, body: str) -> str:
        name = _sanitise(hint)
        candidate = name
        suffix = 1
        while candidate in self._rules and self._rules[candidate] != body:
            suffix += 1
            candidate = f"{name}-{suffix}"
        self._rules[candidate] = body
        return candidate

    def _reserve(self, rules: dict[str, str]) -> None:
        """Add rules under names fixed in advance, or refuse.

        Everything else here goes through :meth:`_define`, which renames around
        a clash.  A pattern's rules cannot: they refer to each other by name,
        so a renamed one would leave a dangling reference.  A schema property
        that sanitises to one of these names and wants a different body is rare
        enough to refuse and much too quiet to overwrite.
        """

        for name, body in rules.items():
            if self._rules.setdefault(name, body) != body:
                raise _Unsupported(f"a pattern rule whose name is already taken: {name}")

    def build(self, node: Any, hint: str) -> str:
        """Define whatever rules ``node`` needs and return the name of its own."""

        if not isinstance(node, dict):
            raise _Unsupported("schema node is not an object")
        if "$ref" in node:
            return self._reference(str(node["$ref"]))
        if "const" in node:
            return self._define(hint, _literal(_json_scalar(node["const"])))
        if "enum" in node:
            values = node["enum"]
            if not values or not all(isinstance(item, str) for item in values):
                raise _Unsupported("only string enumerations are converted")
            return self._define(
                hint, " | ".join(_literal(_json_scalar(item)) for item in values)
            )
        if "anyOf" in node or "oneOf" in node:
            options = node.get("anyOf") or node.get("oneOf")
            if not isinstance(options, list) or not options:
                raise _Unsupported("empty union")
            names = [
                self.build(option, f"{hint}-{index}")
                for index, option in enumerate(options)
            ]
            return self._define(hint, " | ".join(names))
        kind = node.get("type")
        if kind in _PRIMITIVE:
            return self._bounded(str(kind), node, hint)
        if kind == "array":
            return self._array(node, hint)
        if kind == "object":
            return self._object(node, hint)
        raise _Unsupported(f"unconverted schema type {kind!r}")

    def _bounded(self, kind: str, node: dict[str, Any], hint: str) -> str:
        """A bounded scalar, defining a rule for the bounds that need one.

        A length-bounded string is one of the few bounds that can be written
        exactly rather than approximated, which is what this module requires
        before it will carry a bound at all: GBNF counts repetitions, so
        ``minLength``/``maxLength`` become ``char{low,high}`` and the sampler
        cannot leave the range. Anything else still goes to
        :func:`_bounded_primitive`, which refuses what it cannot write.
        """

        bounds = {key: node[key] for key in _BOUND_KEYS if key in node}
        writable = {"minLength", "maxLength", "pattern"}
        if kind != "string" or not bounds or not set(bounds) <= writable:
            return _bounded_primitive(kind, node)
        if bounds == {"minLength": 1}:
            return _bounded_primitive(kind, node)  # the shared nonempty-string rule
        unit = "char"
        pattern = bounds.get("pattern")
        if pattern is not None:
            whole = _PATTERN_STRINGS.get(str(pattern))
            if whole is not None:
                if set(bounds) != {"pattern"}:
                    raise _Unsupported("a structural pattern beside a length bound")
                top, rules = whole
                self._reserve(rules)
                return self._define(f"{top}-string", f'"\\"" {top} "\\""')
            known = _PATTERN_CHARS.get(str(pattern))
            if known is None:
                # Refused rather than approximated, on the same reasoning as
                # every other bound here: a pattern written loosely admits a
                # reply the schema then rejects.
                raise _Unsupported(f"a string matching {pattern!r}")
            unit, body = known
            self._reserve({unit: body})
        low = int(bounds.get("minLength", 0))
        high = bounds.get("maxLength")
        high = None if high is None else int(high)
        if high is not None and high < low:
            # Nothing satisfies it, so no reply could ever be written.
            raise _Unsupported("a string whose maxLength is below its minLength")
        if high == 0:
            if pattern is not None:
                raise _Unsupported("a string bounded to empty against a pattern")
            return self._define("empty-string", '"\\"" "\\""')
        repeat = _repetition(unit, low, high)
        name = f"string-{low}-{'' if high is None else high}"
        return self._define(name, f'"\\"" {repeat} "\\""')

    def _reference(self, ref: str) -> str:
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            raise _Unsupported(f"unresolvable reference {ref!r}")
        key = ref[len(prefix) :]
        existing = self._named_defs.get(key)
        if existing is not None:
            return existing
        target = self._defs.get(key)
        if target is None:
            raise _Unsupported(f"reference to a missing definition {key!r}")
        # Registered before the body is built, so a definition that refers to
        # itself resolves to its own rule instead of recurring for ever.
        name = _sanitise(key)
        self._named_defs[key] = name
        built = self.build(target, key)
        if built != name:
            self._rules[name] = built
        return name

    def _array(self, node: dict[str, Any], hint: str) -> str:
        prefix = node.get("prefixItems")
        if prefix is not None:
            return self._tuple(node, prefix, hint)
        items = node.get("items")
        if items is None:
            raise _Unsupported("an array without a declared item shape")
        item = self.build(items, f"{hint}-item")
        low = int(node.get("minItems", 0))
        high = node.get("maxItems")
        top = int(high) if high is not None else None
        if top is not None and top < low:
            raise _Unsupported("an array that can hold nothing")
        if top == 0:
            return self._define(hint, '"[" "]"')
        repeat = _repetition(f'("," {item})', max(low - 1, 0), None if top is None else top - 1)
        body = f'"[" {item} {repeat} "]"' if repeat else f'"[" {item} "]"'
        if low == 0:
            body = f'"[" ({item} {repeat})? "]"' if repeat else f'"[" ({item})? "]"'
        return self._define(hint, body)

    def _tuple(self, node: dict[str, Any], prefix: Any, hint: str) -> str:
        """A fixed-length array whose members each have their own shape.

        Only the closed case is written: as many members as ``prefixItems``
        declares, no more and no fewer, each converted in place.  A tuple that
        may be short, or that may be followed by further items of some other
        shape, is a union over lengths -- writable, but not without deciding
        what an absent member means, so it is refused instead.
        """

        if not isinstance(prefix, list) or not prefix:
            raise _Unsupported("an array with an empty prefixItems")
        if node.get("items") not in (None, False):
            raise _Unsupported("a tuple that may carry further items")
        size = len(prefix)
        if node.get("minItems") != size or node.get("maxItems") != size:
            raise _Unsupported("a tuple whose length is not fixed by its prefixItems")
        members = [
            self.build(member, f"{hint}-{index}") for index, member in enumerate(prefix)
        ]
        joined = ' "," '.join(members)
        return self._define(hint, f'"[" {joined} "]"')

    def _object(self, node: dict[str, Any], hint: str) -> str:
        properties = node.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise _Unsupported("an object without declared properties")
        declared = set(node.get("required", ()))
        required = [key for key in properties if key in declared]
        optional = [key for key in properties if key not in declared]
        if not required:
            # With nothing required, the opening brace may be followed by any
            # member or by none, so the comma belongs to whichever happens to
            # come first rather than to a fixed position. That is a different
            # and much larger grammar, and no schema here needs it.
            raise _Unsupported("an object with no required properties")
        # Optional members are emitted in a fixed order after the required
        # ones. Every subset is still legal, which is what the schema means,
        # but only in this order -- and since the grammar is what decodes the
        # reply, the model cannot want a different one. This is how
        # llama.cpp's own converter handles the same case. Without it a
        # schema with one optional field falls back to no grammar at all,
        # which costs far more than the ordering does: the local grammar is
        # what strips the whitespace rules, 17.8% of a measured reply.
        parts: list[str] = []
        for index, key in enumerate(required):
            value = self.build(properties[key], f"{hint}-{key}")
            separator = _literal(("," if index else "") + f'"{key}":')
            parts.append(f"{separator} {value}")
        for key in optional:
            value = self.build(properties[key], f"{hint}-{key}")
            parts.append(f"({_literal(f',\"{key}\":')} {value})?")
        return self._define(hint, '"{" ' + " ".join(parts) + ' "}"')

    def text(self, root: str) -> str:
        lines: list[str] = []
        if root != "root":
            lines.append(f"root ::= {root}")
        lines.append(_COMMON.rstrip("\n"))
        lines.append(_BOUNDED.rstrip("\n"))
        lines.extend(f"{name} ::= {body}" for name, body in self._rules.items())
        return "\n".join(lines) + "\n"


_BOUND_KEYS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "multipleOf",
        "pattern",
    }
)


def _bounded_primitive(kind: str, node: dict[str, Any]) -> str:
    """The rule for a scalar, narrowed to whatever the schema bounds it to.

    Only the bounds this can write exactly are accepted.  Anything else is
    refused rather than approximated: a rule wider than the schema loses a whole
    reply to one out-of-range digit, and one narrower makes a correct reply
    unwritable.
    """

    bounds = {key: node[key] for key in _BOUND_KEYS if key in node}
    if not bounds:
        return _PRIMITIVE[kind]
    if kind == "string" and bounds == {"minLength": 1}:
        return "nonempty-string"
    if kind == "integer":
        if bounds == {"minimum": 1}:
            return "positive"
        if bounds == {"minimum": 0}:
            return "natural"
    if kind == "number" and bounds == {"minimum": 0, "maximum": 1}:
        return "unit"
    raise _Unsupported(f"a {kind} bounded by {sorted(bounds)}")


def _json_scalar(value: Any) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    raise _Unsupported("only string constants are converted")


def _repetition(unit: str, low: int, high: int | None) -> str:
    if high == 0:
        return ""
    if low == 0 and high is None:
        return f"{unit}*"
    if low == high:
        return f"{unit}{{{low}}}"
    if high is None:
        return f"{unit}{{{low},}}"
    return f"{unit}{{{low},{high}}}"


def gbnf_from_schema(schema: dict[str, Any]) -> str | None:
    """The grammar for ``schema``, or ``None`` if it is not one this converts."""

    if not isinstance(schema, dict):
        return None
    grammar = _Grammar(schema.get("$defs") or {})
    try:
        root = grammar.build(
            {key: value for key, value in schema.items() if key != "$defs"}, "root"
        )
        return grammar.text(root)
    except _Unsupported:
        return None
    except RecursionError:
        return None
