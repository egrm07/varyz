"""Adversarial privacy release-gate evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from varyz.privacy.models import (
    ConsentGrant,
    ContextItem,
    ContextRequest,
    PolicyEffect,
    PolicyRule,
)
from varyz.privacy.policy import PolicyEngine


@dataclass(frozen=True)
class PrivacyEvaluation:
    cases: int
    unauthorized_sensitive_egress: int
    allowed_items: int
    blocked_items: int

    @property
    def release_gate_passed(self) -> bool:
        return self.unauthorized_sensitive_egress == 0


def evaluate(
    engine: PolicyEngine,
    cases: list[
        tuple[
            ContextRequest,
            tuple[ContextItem, ...],
            tuple[PolicyRule, ...],
            tuple[ConsentGrant, ...],
        ]
    ],
) -> PrivacyEvaluation:
    unauthorized = 0
    allowed = 0
    blocked = 0
    for request, items, rules, grants in cases:
        decisions = engine.evaluate(request, items, rules=rules, grants=grants)
        for item, decision in zip(items, decisions, strict=True):
            leaves_machine = decision.effect in {
                PolicyEffect.ALLOW,
                PolicyEffect.TRANSFORM,
                PolicyEffect.AGGREGATE_ONLY,
            }
            allowed += int(leaves_machine)
            blocked += int(not leaves_machine)
            if leaves_machine and str(item.sensitivity) in {
                "sensitive",
                "unclassified",
            }:
                unauthorized += 1
    return PrivacyEvaluation(
        cases=len(cases),
        unauthorized_sensitive_egress=unauthorized,
        allowed_items=allowed,
        blocked_items=blocked,
    )
