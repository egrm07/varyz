"""What the user is shown before anything reaches a cloud model.

The broker already decides correctly and records a receipt either way. This is
the other half of the contract: the person has to be able to *see* the decision
before it happens, in the terms the release plan names — how much was selected
and how much was withheld, from which sources, about whom, what was removed,
where it is going, why, what was done to it, and the hash of the exact bytes.

It derives everything from the `PreparedEgress` and the candidates that went
into it. It reads no database and holds no plaintext of its own: the counts and
names come from items already selected for disclosure, so displaying this
preflight cannot reveal anything the payload would not.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from varyz.privacy import categories
from varyz.privacy.models import ContextItem, PolicyEffect, PreparedEgress
from varyz.privacy.policy import item_categories

WITHHELD_EFFECTS = {PolicyEffect.DENY, PolicyEffect.LOCAL_ONLY}


class DisclosurePreflight(BaseModel):
    """One screen's worth of fact about a disclosure that has not happened yet."""

    model_config = ConfigDict(frozen=True)

    destination: str
    purpose: str
    intent: str
    selected_items: int
    withheld_items: int
    char_count: int
    source_types: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    includes_names: bool = False
    includes_raw_quotes: bool = False
    credentials_removed: tuple[str, ...] = ()
    confidential_categories: tuple[str, ...] = ()
    transformations: dict[str, str] = Field(default_factory=dict)
    withheld_reasons: tuple[str, ...] = ()
    payload_hash: str
    approval_required: bool
    blocked: bool

    @property
    def credential_count(self) -> int:
        return len(self.credentials_removed)

    def render(self) -> str:
        """The block a person reads. Plain text, no colour, no truncation of
        anything that would change the meaning of the number above it."""
        lines = [
            f"Destination     {self.destination}",
            f"Purpose         {self.purpose}",
            f"Question        {self.intent}",
            f"Sending         {self.selected_items} excerpt(s), {self.char_count} characters",
            f"Keeping local   {self.withheld_items} excerpt(s)",
        ]
        if self.withheld_reasons:
            lines.append(f"  because       {', '.join(self.withheld_reasons)}")
        lines.append(
            f"Source types    {', '.join(self.source_types) if self.source_types else 'none'}"
        )
        if self.people:
            lines.append(f"People named    {', '.join(self.people)}")
        else:
            lines.append("People named    none identified")
        lines.append(
            "Raw quotes      "
            + ("yes, verbatim from your archive" if self.includes_raw_quotes else "no")
        )
        if self.transformations:
            applied = ", ".join(
                f"{name} v{version}" for name, version in sorted(self.transformations.items())
            )
            lines.append(f"Transformed by  {applied}")
        if self.credentials_removed:
            lines.append(f"Secrets removed {', '.join(self.credentials_removed)}")
        else:
            lines.append("Secrets removed none detected")
        if self.confidential_categories:
            lines.append(
                "Confirm first   this includes "
                + categories.describe(self.confidential_categories)
                + " evidence"
            )
        lines.append(f"Payload hash    {self.payload_hash}")
        return "\n".join(lines)


def build_preflight(
    prepared: PreparedEgress, considered: Sequence[ContextItem] = ()
) -> DisclosurePreflight:
    by_id = {item.item_id: item for item in considered}
    outgoing = [by_id.get(item.item_id) for item in prepared.items]

    withheld = [d for d in prepared.decisions if d.effect in WITHHELD_EFFECTS]
    reasons: list[str] = []
    for decision in withheld:
        for code in decision.reason_codes:
            if code not in reasons and not code.startswith(("rule:", "grant:")):
                reasons.append(code)

    sources: list[str] = []
    people: list[str] = []
    found_categories: list[str] = []
    for candidate in outgoing:
        if candidate is None:
            continue
        if candidate.source and candidate.source not in sources:
            sources.append(candidate.source)
        raw = candidate.metadata.get("people", ())
        for person in (raw,) if isinstance(raw, str) else raw:
            if person and str(person) not in people:
                people.append(str(person))
        for name in item_categories(candidate):
            if name not in found_categories:
                found_categories.append(name)

    return DisclosurePreflight(
        destination=prepared.request.destination,
        purpose=prepared.request.purpose,
        intent=prepared.request.intent,
        selected_items=len(prepared.items),
        withheld_items=len(withheld),
        char_count=prepared.char_count,
        source_types=tuple(sorted(sources)),
        people=tuple(sorted(people)),
        includes_names=bool(people),
        # An untransformed item is the archive's own words, unaltered beyond the
        # credential strip. That is the thing worth warning about.
        includes_raw_quotes=any(item.transformation is None for item in prepared.items),
        credentials_removed=prepared.credentials_removed,
        confidential_categories=tuple(
            name for name in categories.LABELS if name in found_categories
        ),
        transformations=dict(prepared.transformation_versions),
        withheld_reasons=tuple(reasons),
        payload_hash=prepared.payload_hash,
        approval_required=prepared.approval_required,
        blocked=prepared.blocked,
    )
