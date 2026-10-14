"""Name the check that refused a provider's reply without repeating what it said.

A model's output is untrusted text.  It is also, on this machine, made of the
owner's own archive: an extraction reply quotes the mail and messages it was
given, and a proposition is a sentence somebody wrote to somebody else.  So a
failure on this boundary must be describable without being quotable.  Pydantic
does not observe that distinction -- a rendered ``ValidationError`` carries
``input_value=``, which is the reply itself, and any field name the model
invented sits in ``loc`` -- and ``ValidationError`` is a subclass of
``ValueError``, so a handler written for one catches the other and an
``f"...: {exc}"`` puts the archive into a log kept for the whole run.

What is safe to repeat is the error ``type``, which is a closed pydantic
vocabulary, and the message of a ``value_error``, which is the literal string
one of our own validators raised.

A :class:`~varyz.llm.request_deadline.LocalModelRequestError` is safe for the
same reason and is repeated in full.  Every one of its messages is composed in
that module out of a server name, a deadline in seconds, an HTTP status and the
text of a transport exception; none of them has the reply or the prompt to
quote.  Reducing it to a type name cost an afternoon on 2026-09-06: two
compiles logged ``LocalModelRequestError: LocalModelRequestError`` and nothing
else, while the message being thrown away said the server had exited
mid-request.  The cause -- ``CUDA error: out of memory`` on a 12 GB card asked
to serve six slots -- was only found by reading llama.cpp's own log.
"""

from __future__ import annotations

from pydantic import ValidationError

from varyz.llm.request_deadline import LocalModelRequestError

__all__ = ["ReplyTruncated", "read_failure_detail", "refusal_reason"]


def refusal_reason(exc: BaseException) -> str:
    """The reasons a reply was refused, deduplicated, in the order they were raised."""

    if isinstance(exc, LocalModelRequestError):
        return str(exc) or type(exc).__name__
    if not isinstance(exc, ValidationError):
        return type(exc).__name__
    reasons = [
        str(error.get("msg", ""))
        if error.get("type") == "value_error"
        else _bounded(error)
        for error in exc.errors()
    ]
    return ", ".join(dict.fromkeys(reason for reason in reasons if reason)) or "unknown"


def _bounded(error: object) -> str:
    """A pydantic error type, carrying the numbers that decided it.

    ``too_long`` alone sends the reader to the schema to guess which field ran
    over -- concept 12 of the compile of 2026-09-07 was refused with exactly
    that and nothing else, and the only way to learn which array had overrun
    was to re-run it.  Pydantic already knows: a bounds failure carries the
    limit and what arrived in ``ctx``.

    Only numbers are repeated.  ``ctx`` is a dict pydantic fills, and for a
    bounds error its values are the constraint and the measured length; a
    string in there could be a default, a discriminator value or anything else
    the reply carried, so nothing that is not an ``int`` or a ``float`` leaves
    this function.  ``bool`` is excluded with them: it is an ``int`` in Python
    and says nothing a reader wants.
    """

    if not isinstance(error, dict):
        return "unknown"
    name = str(error.get("type", "unknown"))
    ctx = error.get("ctx")
    if not isinstance(ctx, dict):
        return name
    numbers = [
        f"{key}={value}"
        for key, value in ctx.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    ]
    return f"{name} ({', '.join(numbers)})" if numbers else name


def read_failure_detail(exc: BaseException) -> str:
    """Describe a failure raised while *reading* a reply.

    Narrower than :func:`refusal_reason`, and only correct where the exception
    came from parsing: there, the one type that embeds the reply is
    ``ValidationError``.  A ``json.JSONDecodeError`` names a position, and a
    ``ValueError`` or ``TypeError`` is a literal string one of our own readers
    raised -- both are worth keeping, and reducing them to a type name throws
    away the only thing that says which check failed.

    Do not use this on an exception that may have come from a transport or any
    other library: their messages can carry whatever they were handed.
    """

    if isinstance(exc, ValidationError):
        return refusal_reason(exc)
    return str(exc) or type(exc).__name__


class ReplyTruncated(ValueError):
    """A reply that stopped because it ran out of room, not because it was done.

    A structured reply cut short is invalid JSON, and every layer above
    reported it as invalid JSON -- which sends the reader looking at the
    model's instruction-following when the fix is a number.  The distinction is
    in the response the whole time (``finish_reason``); it was simply thrown
    away.

    The prompt size is here because it decides *which* number.  A reply that
    stopped at its cap wants a larger cap; a reply that stopped well under it
    ran out of slot, and no cap will buy a token back -- only a smaller
    request will.  Told apart on 2026-08-31: 5,561 tokens produced against a
    cap of 8,192, with a 10,968-token prompt in front of it in a 16,384-token
    slot.

    A ``ValueError``, so existing handlers catch it, and carrying only token
    counts, which are numbers.
    """

    def __init__(self, *, produced: int, cap: int, prompt: int = 0) -> None:
        room = "its token cap" if produced >= cap else "the room left in its slot"
        super().__init__(
            f"the reply was cut at {room} after {produced} of {cap} tokens, "
            f"behind a prompt of {prompt}"
        )
        self.produced = produced
        self.cap = cap
        self.prompt = prompt
