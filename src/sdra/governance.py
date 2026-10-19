"""Classify proposed actions into autonomy tiers and approver roles.

Pure functions over the policy and gateway-computed facts. Facts come from records,
never from the LLM. A rule that needs a fact the gateway did not supply raises
instead of guessing (fail closed).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, NamedTuple

from sdra.policy import REQUIRED_APPROVERS, TIER_RANK, Policy, Rule, TierSpec
from sdra.schema import ActionKind, Model, Tier

Facts = Mapping[str, bool | Decimal]


class MissingFact(KeyError):
    pass


class _Spec(NamedTuple):  # a rule's tier and full approver list, without TierSpec's count check
    tier: Tier
    approvers: list[str]


class Classification(Model):
    tier: Tier
    roles: list[str]  # most senior first; length 0 (execute/blocked), 1 (approve) or 2 (dual)
    rule_ids: list[str]  # rules that set the tier, for the audit log
    rule_approvers: dict[str, list[str]]  # approver roles of each winning rule, for bundling


def _matches(rule: Rule, kind: ActionKind, facts: Facts) -> bool:
    if kind not in rule.actions:
        return False
    for name, threshold in rule.when.model_dump(exclude_none=True).items():
        fact_name = name.removesuffix("_gte")
        if fact_name not in facts:
            raise MissingFact(f"rule {rule.id!r} needs fact {fact_name!r}")
        value = facts[fact_name]
        if name.endswith("_gte"):
            if Decimal(value) < Decimal(threshold):
                return False
        elif value is not threshold:
            return False
    return True


def _resolve(policy: Policy, specs: Iterable[tuple[str, TierSpec]]) -> Classification:
    """Strictest tier wins; signatures come from the winning tier's rules only, capped at
    that tier's count (approve 1, dual 2).

    One signer per kind of risk: each winning rule first contributes its most senior role,
    so a spend rule and a customer-date rule yield one signer each. Remaining places are
    filled from the other roles those rules name, most senior first."""
    specs = list(specs)
    tier = max((spec.tier for _, spec in specs), key=TIER_RANK.__getitem__)
    winning = [(rule_id, spec) for rule_id, spec in specs if spec.tier == tier]
    rank = list(policy.roles).index  # policy.yaml lists roles most senior first
    heads = sorted({min(spec.approvers, key=rank) for _, spec in winning if spec.approvers}, key=rank)
    rest = sorted({r for _, spec in winning for r in spec.approvers} - set(heads), key=rank)
    return Classification(
        tier=tier,
        roles=(heads + rest)[: REQUIRED_APPROVERS[tier]],
        rule_ids=[rule_id for rule_id, _ in winning],
        rule_approvers={rule_id: list(spec.approvers) for rule_id, spec in winning},
    )


def classify(policy: Policy, kind: ActionKind, facts: Facts) -> Classification:
    matched = [(r.id, r) for r in policy.autonomy.rules if _matches(r, kind, facts)]
    if not matched:
        matched = [("default", policy.autonomy.default)]
    return _resolve(policy, matched)


def classify_bundle(policy: Policy, items: Iterable[Classification]) -> Classification:
    """One approval request covers a bundle of actions (e.g. new PO + reduce the late PO)."""
    specs = [
        (rule_id, _Spec(c.tier, c.rule_approvers.get(rule_id, [])))
        for c in items
        for rule_id in c.rule_ids
    ]
    if not specs:
        raise ValueError("empty bundle")
    return _resolve(policy, specs)
