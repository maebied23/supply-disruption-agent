"""Typed loader for a company pack's policy.yaml.

Unknown keys are rejected everywhere, so a misspelt condition or cap fails loudly
instead of silently weakening a control.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from sdra.schema import ActionKind, CurrencyCode, Model, PenaltyTerms, Pct, Tier

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BaseAmount = Decimal  # base-currency amount

TIER_RANK: dict[str, int] = {"execute": 0, "approve": 1, "dual": 2, "blocked": 3}
REQUIRED_APPROVERS: dict[str, int] = {"execute": 0, "approve": 1, "dual": 2, "blocked": 0}


class Company(Model):
    name: str
    base_currency: CurrencyCode
    timezone: str
    workweek: list[Weekday] = Field(min_length=1)
    holidays: list[date] = Field(default_factory=list)
    languages: list[str] = Field(min_length=1)


class Conditions(Model):
    commitment_base_gte: BaseAmount | None = None
    incremental_cost_base_gte: BaseAmount | None = None
    changes_customer_date: bool | None = None
    creates_downstream_shortage: bool | None = None
    item_critical: bool | None = None
    supplier_approved_for_item: bool | None = None
    within_mandate: bool | None = None
    requires_customer_consent: bool | None = None
    customer_consent_recorded: bool | None = None
    template_only: bool | None = None
    hard_allocation: bool | None = None


class TierSpec(Model):
    tier: Tier
    approvers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _approver_count(self) -> TierSpec:
        need = REQUIRED_APPROVERS[self.tier]
        if len(self.approvers) != need:
            raise ValueError(f"tier {self.tier!r} needs exactly {need} approver role(s)")
        if len(set(self.approvers)) != len(self.approvers):
            raise ValueError("approver roles must be distinct")
        return self


class Rule(TierSpec):
    id: str
    actions: list[ActionKind] = Field(min_length=1)
    when: Conditions = Field(default_factory=Conditions)


class Autonomy(Model):
    default: TierSpec
    rules: list[Rule]

    @model_validator(mode="after")
    def _unique_ids(self) -> Autonomy:
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate rule id")
        return self


class Caps(Model):
    per_incident_commitment_base: BaseAmount = Field(gt=0)
    per_day_commitment_base: BaseAmount = Field(gt=0)
    per_incident_incremental_cost_base: BaseAmount = Field(gt=0)
    max_actions_per_incident: int = Field(gt=0)
    max_replans_per_incident: int = Field(ge=0)


class VoidIfChanged(Model):
    stock_on_hand_pct: Pct
    offer_price_pct: Pct
    any_customer_promise_date: bool
    any_po_expected_date: bool


class Approvals(Model):
    expiry_hours: int = Field(gt=0)
    reminder_after_hours: int = Field(gt=0)
    reproposals_after_rejection: int = Field(ge=0)
    revalidate_on_resume: Literal[True]  # not switchable off: stale approvals are a known failure
    void_if_changed: VoidIfChanged

    @model_validator(mode="after")
    def _reminder_before_expiry(self) -> Approvals:
        if self.reminder_after_hours >= self.expiry_hours:
            raise ValueError("reminder must come before expiry")
        return self


class Silence(Model):
    confirmation_due_days_before_expected: int = Field(ge=0)


class Extraction(Model):
    min_confidence: float = Field(ge=0, le=1)
    require_sender_match: bool
    require_po_reference_resolves: bool
    allow_qty_above_open_po: bool
    require_values_in_quote: bool  # claimed quantity and date must be readable from the quoted evidence
    max_date_shift_days: int = Field(gt=0)
    follow_up_hours: int = Field(gt=0)
    contradiction_window_hours: int = Field(gt=0)
    silence: Silence


class MandatePolicy(Model):
    premium_ceiling: Literal["next_best_alternative"]
    safety_margin_pct: Pct
    validity_hours: int = Field(gt=0)


class Reallocation(Model):
    protect_priority_tiers: list[int] = Field(default_factory=list)


class Scoring(Model):
    objective: Literal["min_expected_total_cost"]
    goodwill_cost_per_late_day_by_tier: dict[int, BaseAmount]
    unfilled_late_days: int = Field(gt=0)  # lateness assumed for a line the plan never completes


class Options(Model):
    on_best_option_blocked: Literal["escalate"]  # never fall back silently to a worse option
    late_po_quantities_cancellable: bool  # contract term: late, undelivered quantity can be cancelled
    enabled: list[
        Literal["reallocate_stock", "buy_from_backup", "substitute", "partial_delivery", "accept_delay"]
    ] = Field(min_length=1)
    reallocation: Reallocation
    scoring: Scoring


class Communication(Model):
    outbox_only: Literal[True]  # no real sending in this project
    templates_dir: str
    draft_languages: list[str] = Field(min_length=1)
    never_disclose: list[
        Literal[
            "internal_cost",
            "penalty_terms",
            "mandate",
            "other_supplier_offers",
            "customer_identity_to_suppliers",
            "supplier_identity_to_customers",
        ]
    ]


class Routing(Model):
    """Who receives work the agent cannot finish. Role names come from `roles`."""

    review: str  # unreadable or unverifiable messages, silence, refused autonomous actions
    blocked_option: str  # best option blocked by a rule (e.g. outside its mandate): choose how to proceed
    blocked_by_rule: dict[str, str] = Field(default_factory=dict)  # rule id -> role, overrides blocked_option
    escalation: str  # re-plan cap reached, gateway refusal, repeated rejection


class Concession(Model):
    tactic: Literal["time_dependent"]
    beta: float = Field(gt=0)


class Negotiation(Model):
    max_rounds: int = Field(gt=0)
    deadline_hours: int = Field(gt=0)
    concession: Concession


class Policy(Model):
    version: Literal[1]
    company: Company
    fx_rates_to_base: dict[CurrencyCode, Decimal]
    roles: dict[str, str] = Field(min_length=1)
    autonomy: Autonomy
    caps: Caps
    approvals: Approvals
    extraction: Extraction
    mandate: MandatePolicy
    options: Options
    default_penalty: PenaltyTerms
    communication: Communication
    routing: Routing
    negotiation: Negotiation | None = None

    @model_validator(mode="after")
    def _cross_checks(self) -> Policy:
        if self.fx_rates_to_base.get(self.company.base_currency) != Decimal(1):
            raise ValueError("fx_rates_to_base must map the base currency to 1")
        if any(rate <= 0 for rate in self.fx_rates_to_base.values()):
            raise ValueError("fx rates must be positive")
        known = set(self.roles)
        specs = [self.autonomy.default, *self.autonomy.rules]
        unknown = {role for spec in specs for role in spec.approvers} - known
        if unknown:
            raise ValueError(f"approver roles not defined in roles: {sorted(unknown)}")
        r = self.routing
        routed = {r.review, r.blocked_option, r.escalation, *r.blocked_by_rule.values()} - known
        if routed:
            raise ValueError(f"routing roles not defined in roles: {sorted(routed)}")
        rule_ids = {rule.id for rule in self.autonomy.rules}
        if set(r.blocked_by_rule) - rule_ids:
            raise ValueError(f"routing.blocked_by_rule names unknown rules: {sorted(set(r.blocked_by_rule) - rule_ids)}")
        return self


def load_policy(path: str | Path) -> Policy:
    with open(path, encoding="utf-8") as f:
        return Policy.model_validate(yaml.safe_load(f))
