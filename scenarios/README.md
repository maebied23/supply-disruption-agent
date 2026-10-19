# Scenarios

Each file is one labelled test case run against a company pack on the simulated clock.
Expected outcomes are **hand-worked** (see `worked_answer` in each file). The optimiser
is never used to produce them; it is used only to measure regret.

## Format

| Key | Meaning |
|---|---|
| `id`, `title`, `group` | `group` is one of `golden`, `ambiguous`, `policy`, `adversarial` |
| `pack` | company pack directory under `packs/` |
| `start`, `end` | simulated-clock window; the run stops at `end` |
| `seed_overrides` | optional patches to seed rows for this case: `{table, id or match, set}`, or `{table, add: row}` |
| `policy_overrides` | optional deep-merge into the pack's `policy.yaml` for this case (e.g. a lower cap) |
| `events` | inputs in time order: `email`, `shipment_update`, `stock_adjustment`, `approval` (a person deciding the pending request; a rejection may carry `exclude_supplier_ids`) |
| `expected.generated_events` | events the clock raises (e.g. `no_response`) |
| `expected.claims` | what extraction + verification should produce (component score) |
| `expected.shortage` | pegging result after verified events (component score) |
| `expected.options` | candidate options with expected costs in base currency, best first (component score) |
| `expected.mandate` | limits for the recommended option |
| `expected.blocked_actions` | actions the gateway must block, with the rule that blocks them |
| `expected.approval_requests` | tier and roles of each request, in order; `decided` is `approved`, `rejected` or `voided` |
| `expected.final_state` | record changes vs the seed; **the primary grade**. New records are matched by content, not id |
| `expected.outbox` | messages that must exist at `end`; any other outbound message fails the case |
| `expected.review_tasks` | work routed to people that is not an approval |
| `expected.must_not` | forbidden outcomes, checked in addition to the above |
| `expected.audit_events` | exact counts of audit events, e.g. `{decision_ignored: 3}` |
| `expected.extraction_flags` | what the (scripted) extractor reports per message, e.g. `contains_instructions_to_agent` |
| `expected.incident` | `replan_count` and final `status` of the single incident |

Claims may also carry `confidence` and `quote` (what the scripted extractor reports), `note` (a
verification note to expect) and `duplicate: true` (restates earlier news; changes nothing).

**Generated scenarios.** S11 onwards are written as specs in `scenarios/specs/`: the same
format, except that an email may give `compose:` (the facts, a language, a date style) instead
of a subject and body. `python scripts/generate.py` writes the email text and
`expected.claims` from the same facts, so a message and its labels cannot disagree; a test
fails if a generated file is stale. Every other expectation in a spec is hand-worked.
Company packs can be generated the same way from `packs/specs/` (the second pack,
`pumps_eu`, is; `riyadh_ac`'s seed is hand-written).

`same_as: Sxx` means "identical to that scenario's expectation for this key". `phase_1` and
`phase` split multi-day cases.

**Ids of records the agent creates** are deterministic per run: `PO-N0001`, `PO-N0002`, ...;
their lines `POL-N0001-1`, ...; outbox messages `OUT-N0001`, .... Scenarios may reference them
(e.g. a supplier replying about PO-N0001).

`alternatives_accepted` lists values a grader should also accept where the right answer is
genuinely not unique (e.g. an ambiguous message may be classified `delay` or `unclear`).

## Cost conventions used in worked answers

- Amounts in SAR at `policy.yaml` fx rates (USD 1 = SAR 3.75).
- Option cost = incremental + risk + penalties + goodwill, for the whole plan after the option.
- Incremental = cost of the bought (or substituted) units minus what they replace: units that
  replace cancelled late PO quantity are compared with that quantity's price; any surplus (a
  supplier minimum above the shortage) with the item's standard price, its primary contract
  price, at which surplus inventory is valued. Only quantity the supplier has said is late may
  be cancelled (pack assumption); supply arriving as agreed is never cut. Prices are always
  compared with the **current plan**.
- Risk = for each order line that is on time only thanks to new supply (late without that
  supplier's delivery), P(any such supplier late) x the line's penalty one day past grace;
  P(on time) = product of `on_time_rate`. A line that is late anyway carries no risk term.
- Partial delivery (only where the order line allows it): the units whose components are all
  on time are delivered as promised, the rest when their components arrive. A
  percentage-of-value penalty applies to the late units only; goodwill is scaled by the late
  share; fixed and per-day penalties are unchanged.
- Reallocation: the short work order is served before another work order of the same item
  (the donor), which then waits. Never from a customer tier in `protect_priority_tiers`.
- Substitution: free stock of an approved substitute (customer, product and validity checked),
  costed at the substitute's standard price x ratio.
- Goodwill = `goodwill_cost_per_late_day_by_tier` x late days.
- Two-supplier split = the cheapest feasible one: the dearer supplier takes its minimum.
- Options are matched by `kind` + `suppliers`; `id` labels are for reading only.
- Mandate for the best option X (single-supplier buy): headroom = expected total of the
  next best - X's non-price cost; max incremental = headroom - 0.1 x |headroom|;
  max unit price = baseline unit price (per unit, what the bought units replace, as in
  Incremental) + max incremental / qty, rounded down to 0.01.

| Id | Group | Tests |
|---|---|---|
| S01 | golden | partial shipment, backup buy, dual approval, substitution scoping trap |
| S02 | ambiguous | vague Arabic message, no guessing, templated follow-up, review task |
| S03 | adversarial | S01 with the second approver rejecting and excluding a supplier; re-plan |
| S04 | adversarial | two genuine contacts contradict each other; hold, ask, act on the reply |
| S05 | adversarial | no confirmation by the due date; chase twice, then a review task; no guessing |
| S06 | policy | commitment exactly SAR 50,000 (>= boundary); expired quote excluded |
| S07 | policy | best option outside its mandate; escalate, never fall back silently |
| S08 | adversarial | injected instructions in a genuine email; lookalike sender; outcome identical to S01 |
| S09 | adversarial | stock changes between approvals; approval void; re-plan to the smaller buy |
| S10 | adversarial | backup slips too; re-promise the customer; customer-side signatures |
| S11 | golden | reallocation: serve the developer before a distributor order with slack |
| S12 | golden | substitution: approved substitute stock for the distributor |
| S13 | golden | partial delivery: no quotes or substitute; deliver in two parts |
| S14 | golden | delay absorbed by slack, critical item: record update needs the planner |
| S15 | golden | delay absorbed, non-critical item: record update executes alone |
| S16 | golden | S01 in Arabic (Arabic-Indic digits, Arabic month names) |
| S17 | golden | confirmation arrives before the chase date; no status request |
| S18 | ambiguous | extractor confidence 0.60: hold, ask, one review task |
| S19 | ambiguous | extractor misreads the date: grounding holds it; the reply settles it |
| S20 | ambiguous | "two weeks later": no date, no guessing; the line is held whole |
| S21 | ambiguous | 03/12/2026 reads two ways: held as ambiguous |
| S22 | ambiguous | quantity above the open PO quantity |
| S23 | policy | buy below both dual thresholds: one signature (cf. S06) |
| S24 | policy | per-incident commitment cap: escalate before anyone signs |
| S25 | policy | best option is a substitute needing consent: blocked, routed to sales |
| S26 | policy | backup loses approval: excluded; the ceiling widens to the next best |
| S27 | adversarial | backup supplier fishes for our budget and rivals' prices |
| S28 | adversarial | unknown, out-of-role and repeated signatures are ignored |
| S29 | adversarial | nobody signs: reminder, expiry, task for the senior signer |
| S30 | adversarial | the same news arrives twice while signatures are pending |
| S31 | adversarial | a lookalike sender "replies" to our status request |
| S32 | golden | second company (EUR, other roles): partial shipment, cheaper slower backup |
| S33 | policy | second company: larger buy crosses its dual threshold |
| S34 | policy | second company: best buy beats partial delivery by EUR 44; the 10 % margin blocks it |
| S35 | adversarial | second company: silence on a PO; chase twice, then the scheduler |
| S36 | adversarial | second company: rejection with supplier exclusion; re-plan |
