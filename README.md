# Supply Disruption Response Agent

**When a supplier fails to deliver as promised, this agent works out which customer orders are at risk, prices every way out, sends the right people a recommendation they can check in a minute, and carries out whichever fix they sign. It acts only within limits the company writes into a policy file.**

> **V1 results.** 36 hand-labelled scenarios across two fictional companies (different currency, roles, thresholds and penalties, same code) pass every graded component, with **zero** actions beyond authority, **zero** internal-cost leaks and **zero** purchases above the computed price ceiling. The agent's choice equals the best plan found by brute force at all 25 decision points. With 20 % of extracted claims deliberately corrupted, **0 of 170** runs did anything wrong: each either reached the right outcome or held the claim and asked.
>
> Synthetic data and the agent's own cost model: this shows behaviour and safety, not real-world savings.

**Stack:** Python 3.12+, Pydantic, LangGraph (interrupt/resume, SQLite checkpointer), Claude (reading emails only), FastAPI + Next.js approver screen, SQLite. About 5,400 lines of agent code and 297 tests.

---

## Contents

1. [The problem](#1-the-problem)
2. [The approach](#2-the-approach)
3. [Design principles and why](#3-design-principles-and-why)
4. [How it works](#4-how-it-works)
5. [One agent, many companies](#5-one-agent-many-companies)
6. [Worked example: a Riyadh air-conditioner assembler](#6-worked-example-a-riyadh-air-conditioner-assembler)
7. [Evaluation](#7-evaluation)
8. [Running it](#8-running-it)
9. [Limitations](#9-limitations)
10. [Repository map and roadmap](#10-repository-map-and-roadmap)
11. [References](#11-references)

---

## 1. The problem

Manufacturers assemble products from bought-in parts and deliver them by promised dates, often with late-delivery penalties. When a supplier slips, a good response joins up knowledge that sits with different people:

| Question | Who knows today | Typical delay |
|---|---|---|
| Is the message genuine, and what exactly changed? | The buyer whose inbox it reached | hours |
| Which production runs and customer orders depend on it? | The planner, at the next planning run | 1–5 days |
| What are the options, and what does each really cost, including penalties and the risk that the backup is late too? | Nobody in one place; often "expedite" or the first backup to answer wins | 1–2 days |
| Who may approve it? | Thresholds applied from memory, in email chains | hours to days |
| Did the facts change before we acted? Who decided, on what evidence? | Rarely recorded | never |

Eight breakdowns recur:
- signals stuck in personal inboxes;
- impact found late, and traced by hand across two people's knowledge;
- options not compared on cost;
- approvals signed without the evidence attached, then executed after the facts changed;
- supplier silence going unnoticed;
- customers told last;
- no record of why a choice was made.

Speed matters because options expire: a backup with a 21-day lead time stops being an option three weeks before the parts are needed.

Startups in this space mostly chase suppliers and extract order updates. This project covers the whole decision: from the message to the customer orders it threatens, a priced comparison of fixes, a computed limit on what is worth paying, routing to the people entitled to approve, and execution that is re-checked, idempotent and audited.

## 2. The approach

**The model reads; deterministic code decides.** A language model turns each supplier message into structured claims with quotes. Everything after that is ordinary, testable code:
- checking the claims;
- tracing the impact;
- pricing the options and setting the price ceiling;
- deciding who must sign;
- executing.

The model never sets a quantity, price, limit or approval.

```mermaid
flowchart LR
    E[Supplier email] --> X["Read<br/>(Claude, quarantined:<br/>no tools, no records)"]
    X --> V["Verify<br/>sender, verbatim quote,<br/>numbers in the quote, PO, plausibility"]
    V -->|cannot use| Q[Ask the supplier<br/>+ task for a person]
    V --> P["Trace impact<br/>supply to work orders<br/>to customer orders"]
    P --> O["Price every option<br/>+ set the price ceiling"]
    O --> G["Classify against policy.yaml<br/>tier and signers"]
    G --> A["Wait for signatures<br/>(LangGraph interrupt, expiry)"]
    A --> R["Re-check the facts<br/>void if they moved"]
    R --> W["Action gateway<br/>authority, caps, leak check,<br/>run once"]
    W --> DB[(Records)]
    W --> OB[Outbox]
    W --> AU[(Hash-chained audit log)]
    C[Simulated clock:<br/>silence, reminders, expiry] --> V
    C --> A
```

## 3. Design principles and why

| Principle | Why |
|---|---|
| **The model reads; code decides** | LLM inventory managers anchor on averages and amplify order swings (AIM-Bench). Weaker LLM negotiators accept money-losing deals (Liang & Xu). Arithmetic and permissions must be testable and repeatable. |
| **Every inbound message is untrusted data** | Emails can carry instructions aimed at an AI. Extraction is isolated by structure (no tools, no records, fixed output schema; the quarantined-LLM pattern of CaMeL), so an injection can at most produce a claim, and every claim is checked. |
| **Verify, never guess** | Acting on a misread date or "the rest follows two weeks later" is a guess with a penalty attached. Asking costs hours; guessing can cost the order. |
| **Price every option the same way; set the ceiling from the next-best alternative** | Habit ("expedite", the first answer) overpays or buys fixes that don't save the order. A ceiling derived from the best alternative is a defensible limit, and it is what the V2 negotiator will be held to. |
| **Governance in policy, enforced at one choke point** | Levels of automation should be chosen per action (Parasuraman, Sheridan & Wickens). One gateway can be audited; rules spread across prompts cannot. Caps apply to the whole incident, because individually acceptable steps can add up to an unacceptable outcome. |
| **Make checking cheap; re-check before acting** | Explanations alone don't reduce over-reliance; lowering the cost of verification does (Buçinca et al.; Vasconcelos et al.). Approvals executed after the facts changed are a known failure, so approvals are re-validated and voided when stale. |
| **Everything reconstructable** | Manufacturers name traceability as a precondition for trusting agents (Edwards et al.). |
| **Mostly a single agent** | Every hand-off between agents loses information (Cemri et al.). Splits are reserved for least privilege (the V2 negotiator). |
| **General by construction** | Company specifics live in a company pack, never in code; a test enforces it (section 5). |

## 4. How it works

### Intake: from email to trusted claims (`src/sdra/intake/`)

**Extraction** is one Claude call per message, with structured output and no tools. The prompt treats the message as untrusted. It asks for one claim per fact, codes copied exactly, no estimated values, and quotes that contain each claim's numbers. If the message tries to instruct an AI, claims pre-approval or asks for internal prices, the model sets a flag, and the flag only creates a review task.

**Verification** classifies every claim as verified, held, or failed:

| Check | Result if it doesn't pass |
|---|---|
| Sender is a listed contact of that supplier (catches lookalike domains) | failed |
| Every quote appears verbatim in the message | failed |
| The PO exists, belongs to that supplier, and the item is on it | failed |
| The claimed quantity and date can be read from the quotes: English and Arabic month names, Arabic-Indic digits, ISO and day-first formats. "03/12" and "two weeks later" are not accepted | held |
| Confidence at or above the policy minimum; quantity within the open PO; date shift within the policy limit | held |
| Part of the same PO line is unclear | the whole message is held for that line |

**A claims ledger** applies three rules:
- **Contradictions.** Genuine contacts who disagree within the policy window are held, and the supplier's reply to our question settles it.
- **Duplicates.** A message that only repeats what the ledger holds changes nothing.
- **Failed claims** never count as evidence for anything.

**When a claim can't be used,** the agent sends a templated status request and opens one review task stating the exposure. Silence is watched on the simulated clock: unconfirmed POs, including ones the agent placed, are chased twice and then handed to a person.

### The deterministic core (`src/sdra/core/`)

**Pegging** matches supply to need. Supply is usable stock plus open PO lines adjusted by verified claims; quantity no claim accounts for is treated as unavailable. Need is each work order's component requirements on its start date. Supply is allocated by need date and customer tier, which yields each customer order's completion date, lateness and penalty.

**Options** are generated for the most urgent shortage, each enabled per company:

| Option | Guard rails |
|---|---|
| Accept the delay | always listed; it bounds every ceiling |
| Buy from one backup, or the cheapest two-supplier split | quote valid, supplier approved for the item, minimum and maximum quantities respected |
| Reallocate: serve this order before another that has slack | never from a protected customer tier |
| Use an approved substitute in stock | customer, product and validity checked; if consent is required, blocked and routed to sales |
| Partial delivery | only where the contract allows, and only with a date for the rest |

**One cost model** applies to every option:

```
expected total = incremental + risk + expected penalties + goodwill

incremental = cost of the new units - price of the cancelled late supply they replace
              - standard price x any surplus (e.g. a supplier minimum)
risk        = for each order line on time only thanks to new supply:
              P(any such supplier late) x the line's penalty one day past grace
penalties   = contract terms (fixed / per day with cap / % of value per week with cap)
              x the customer's enforcement probability
goodwill    = cost per late day by customer tier
```

**The price ceiling (the mandate)** for a purchase:

```
headroom       = expected total of the next-best option - this option's non-price cost
max extra cost = headroom - safety margin x |headroom|
max unit price = price of what it replaces (per unit) + max extra cost / quantity, rounded down
```

Because waiting is always an option, the ceiling never exceeds the penalty avoided. If the best option is outside its ceiling, the agent escalates to a person; it never falls back silently to the second choice.

### Governance and the workflow (`src/sdra/governance.py`, `src/sdra/agent/`)

`policy.yaml` assigns each action a tier: **execute** (no signature), **approve** (one), **dual** (two) or **blocked** (a person acts outside the agent). The tier depends on conditions the code computes from records: commitment, extra cost, customer date change, part criticality, supplier approval, mandate, consent. The strictest tier wins. There are at most two signers, from distinct people, one per kind of risk, so a large purchase that also moves a customer date is signed by operations *and* sales.

The workflow is a LangGraph graph per incident: *plan, request signatures (one interrupt each, in any order), re-validate, execute*.

| Event | Response |
|---|---|
| Rejection | Remove that option and any supplier the approver excluded, then re-plan once. The free-text comment is recorded, never read. |
| New supplier news, or changed facts at re-validation (stock, quotes, PO and customer dates, the schedule) | The approval is void; re-plan. |
| No signature | Reminder after 4 h; after 24 h it expires and becomes a task for the most senior outstanding signer. |
| Unknown signer, wrong role, same person twice | Ignored and audited. |
| Plan the gateway would refuse | Escalated before anyone is asked to sign. |
| No shortage at all | PO records are still updated to the supplier's dates, under the date rules. |

### The action gateway (`src/sdra/agent/gateway.py`)

Every record write and outbound message passes through one gateway, all or nothing. It checks:
- **Authority:** the right signers, on this request, before it expired. Execute-tier actions are re-classified by the gateway itself.
- **Caps:** spend per incident and per day, and actions per incident.
- **The leak check:** no penalties, costs or price ceiling (in any currency or rounding), no other suppliers' prices or names, no customer names to suppliers, no supplier names to customers.

Each action carries an idempotency key, so a retry, even after a restart, never applies twice. V1 sends only templates filled from records, to an outbox; nothing the model wrote is ever sent.

### Audit, persistence and the approver screen

Every step is appended to a **hash-chained, append-only audit log**: any edit, deletion or reordering is detectable, and the database rejects updates. Records, audit and runner state are saved in SQLite; paused approvals survive a restart.

The **approver screen** (FastAPI + Next.js) is designed to make checking cheap rather than to persuade:
- what changed since the analysis;
- the supplier's words with the quoted span highlighted;
- every alternative as "+X compared with the recommendation", and the options that were not offered, with the reason;
- the price ceiling and the assumptions;
- the exact text of every message that will go out.

The override rate is tracked.

## 5. One agent, many companies

Everything company-specific lives in a **company pack**, and the agent reads it at run time:

- **Records** (`seed.yaml`, or an ERP through the `RecordStore` adapter interface): items and BOMs, stock, suppliers with contacts and on-time history, contract prices and standing backup quotes, customers with tiers and penalty terms, open orders, and approvers.
- **`policy.yaml`**, validated on load (unknown keys are rejected, so a typo can't switch a control off):
  - currency and exchange rates; role names in seniority order;
  - every approval rule and threshold, and caps;
  - approval expiry; how strict extraction is;
  - the safety margin; which fixes are allowed and which customers are protected;
  - goodwill and default penalty;
  - what must never be disclosed, and who receives escalations.

**Enforced, not just intended.** A test collects every company value from every pack (ids, roles, currencies, names, rule ids, time zone) and fails if any appears in agent code. It caught a real customer PO number used as the example in the extraction prompt, and role names written into the code.

**Proven with a second company.** `packs/pumps_eu` is a fictional European pump maker chosen to break hidden assumptions: EUR instead of SAR, a supplier quoting in USD, entirely different role names, lower thresholds, different penalty types and time zone. Its five scenarios pass on unchanged code. Its pack is generated from a compact description ([`packs/specs/pumps_eu.yaml`](packs/specs/pumps_eu.yaml)).

**Setting up a new company** means providing its records (or an ERP adapter), writing its `policy.yaml` with its process owners, and hand-working 5 to 10 of its own scenarios to prove the setup before anything goes live. A pilot then starts in shadow mode: the agent recommends alongside the real planners and changes nothing, and autonomy widens one tier at a time on measured agreement. These currently need a developer rather than configuration:

| Need | Today |
|---|---|
| Delivery days that skip weekends and holidays | the pack records a working week; planning uses calendar days |
| Own message wording or languages | templates are generic English, in code |
| Penalty terms beyond fixed, per day, or % per week | three types |
| A new kind of approval condition | thresholds configurable; new conditions need code |
| Several plants, capacity limits, process manufacturing | outside the V1 model |
| WhatsApp or phone as the main channel | email only; a new input adapter would feed the same checks |

## 6. Worked example: a Riyadh air-conditioner assembler

The default pack (`packs/riyadh_ac`) is a fictional Riyadh company that assembles air conditioners for three business customers:
- a hotel chain: tier 1, a fixed SAR 90,000 late penalty, no partial deliveries;
- a property developer: tier 2, a per-day penalty;
- a distributor: tier 3, a percentage penalty.

It buys compressors from a main supplier and holds standing quotes from two backups. Scenario S01 is the running example, and every number below is hand-worked in its scenario file and reproduced exactly by the code.

**The email.** *"We can only deliver 300 of the 500 units of CMP-24 on PO-4471 on 10 November as confirmed. The remaining 200 units will follow on 20 December."*

1. **Read and verify.** Two claims come back: 300 on time, and 200 moved to 20 Dec. The sender is a listed contact, both quotes are verbatim, the numbers are in the quotes, and the PO is the supplier's with 500 open. Both claims are verified.
2. **Impact.** The hotel's production run needs 600 compressors by 24 Nov and only 400 arrive in time. Partial delivery is forbidden, so the whole order is at risk: SAR 90,000.
3. **Options:**

   | Option | Extra cost | Risk | Penalty + goodwill | **Expected total (SAR)** |
   |---|---|---|---|---|
   | **Buy 200 from backup B** (USD 340, 7 days, 95 % on time) | 30,000 | 4,500 | – | **34,500** |
   | Buy 200 from backup C (USD 315, 21 days, 70 % on time, zero slack) | 11,250 | 27,000 | – | 38,250 |
   | 50 from B + 150 from C | 15,937.50 | 30,150 | – | 46,087.50 |
   | Accept the delay | – | – | 90,000 + 48,000 | 138,000 |

   Not offered, with the reason shown to the approver: a substitute compressor in stock (approved for the distributor only), and partial delivery (forbidden by the hotel's contract).
4. **Ceiling.** The break-even against C, less the 10 % margin and rounded down, is **SAR 1,276.87 per unit**. B quotes SAR 1,275 (USD 340), so it is inside.
5. **Signers.** The commitment is SAR 255,000 (≥ 50,000) and the extra cost SAR 30,000 (≥ 25,000), so two signatures: the operations director and the procurement manager.
6. **Execute.** After both sign and the facts are re-checked, the gateway creates the PO to B, cuts the late 200 from PO-4471, and sends both suppliers template messages that disclose nothing internal. The new PO is then watched for confirmation.
7. **If B slips too** (scenario S10), the incident re-opens. Buying from C is now cheaper than the late supply it replaces, but the hotel's date must move, so the signers become the operations director and the sales manager.

The other scenarios each change one thing in this story; the catalogue is in [`scenarios/README.md`](scenarios/README.md).

## 7. Evaluation

**Method** (after τ-bench):
- grade the final state of the records, the outbox and the audit log against hand-labelled goals, not the transcript;
- score seven components separately, so a failure says where it happened: extraction, core, governance, final state, outbox, review tasks, safety;
- measure consistency over repeated runs.

**Expected answers are worked out by hand.** The optimiser never produces them, which would be circular; it is used only to measure regret.

### How the 36 scenarios were designed

There are 9 golden, 6 ambiguous, 8 policy and 13 adversarial scenarios; 31 use the Riyadh pack and 5 the pump pack.

- **From the breakdowns, before the code.** Each of the breakdowns in section 1 became a scenario before the code that handles it existed. The first ten were the go/no-go gate.
- **A checkable trade-off.** S01 uses round numbers so every cost can be worked on paper, and a real dilemma: B is dear and reliable, C cheap, shaky and with zero slack. It also plants two traps a careless system would use: a substitute approved for another customer, and a contract that forbids partial delivery.
- **One change per variant:** a rejection (S03), injected instructions plus a lookalike sender (S08), stock changing between signatures (S09), Arabic (S16), a lower cap (S24), forged signatures (S28), nobody signing (S29), a duplicate email (S30). When a variant fails and S01 passes, the cause is that one difference.
- **Boundaries on the edge:** exactly SAR 50,000 (S06, two signatures) against just below it (S23, one). A best option just outside its ceiling (S07). A purchase that beats partial delivery by only EUR 44, so the safety margin decides (S34).
- **What planners hate:** a record left showing an old date (S14), a backup fishing for our budget (S27), a lookalike "reply" to our own question (S31), a misread date (S19), "two weeks later" (S20), and "03/12/2026" (S21).
- **Messages and labels from one source.** From S11 on, a generator writes each email from its facts, in English or Arabic with varied wording and date styles, and writes the expected claims from the same facts. Every other expectation is hand-worked.

### Results

All of these are generated by `python scripts/evaluate.py` into [`docs/evaluation.md`](docs/evaluation.md), deterministically.

- **Suite:** 36/36 pass all seven components.
- **Safety: zero violations**, on checks that need no answer key:
  - every change traces back to enough real signatures;
  - no secret number appears in any sent message;
  - nothing was bought above its ceiling;
  - the audit chain is intact.

**Baselines,** at each decision, using the agent's own cost model:

| Policy | Riyadh (SAR, 21 decisions) | Pumps (EUR, 4 decisions) |
|---|---|---|
| **Agent** | **592,900** | **6,926** |
| Always buy from the fastest backup | 657,340 | 8,772 |
| First come, first served, no purchase | 2,428,000 | 121,120 |
| Always wait | 2,442,000 | 121,120 |
| Brute-force best plan (any quantities from one or two offers, combined with reallocation, substitution and partial delivery) | 592,900 | 6,926 |

**Regret is zero** at all 25 decisions, so the allocation optimiser the plan allowed for was not built.

**Robustness to extraction errors (no model cost).** Each claim was corrupted with probability 0.2: dropped, date shifted, quantity scaled, confidence lowered, PO digits swapped, or an invented claim added. Each scenario ran 5 times, 170 runs in all: **127 correct, 43 safe but incomplete** (held and asked), **0 wrong**, 0 safety violations.

**Safety margin sweep:**

| Margin | Escalations | Purchases that went ahead | Of those, worse once the inputs are perturbed |
|---|---|---|---|
| 0 % / 5 % | 0 | 21 | 24 of 147 cases |
| **10 % (kept)** | 2 | 19 | 20 of 133 cases |

The 10 % margin escalates two close calls, which proved the worse buy in 4 of 14 perturbed cases. Most exposure to estimation error lies in rankings the margin can't change; better on-time data is the lever there.

**Mutation checks.** On each build day, every new safeguard was broken on purpose to confirm a test notices. On day 5, all 14 breaks were caught.

### What the evaluation caught

| Found by | Defect | Fix |
|---|---|---|
| Testing approvals while new emails arrive | A contradicting email didn't void a pending approval | Supplier claims are part of the approval basis |
| Hand-working a substitution case | Risk charged at the wrong customer's penalty (4,500 instead of 420) | Risk counts only for orders that would be late without that supplier |
| The second company | A backup purchase cut a PO arriving on time; the message promised a moved date | Only declared-late quantity is cancellable |
| A misread-date scenario | Numbers were never checked against the quote | The grounding check |
| A duplicate-email scenario | A forwarded copy voided the approval | Duplicate detection |
| The regret search | Surplus units counted as savings | Surplus valued at the standard price |
| The second company | A quantity of 200 matched a EUR 200 cost and blocked a harmless message | Template quantities are not read as amounts |
| Noisy-extraction runs | A crash on a partial delivery with no date for the rest | Option excluded; planning failures escalate |
| The lint test | A real PO number in the prompt; role names in code | Moved to the pack |

## 8. Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest                                  # 297 tests
python -m pytest spikes                 # LangGraph interrupt/resume spike (5 checks)
python scripts/evaluate.py              # regenerates docs/evaluation.md
python scripts/generate.py --check      # generated packs and scenarios are current

# approver screen
PYTHONPATH=src uvicorn sdra.api.app:app --port 8000
cd ui && npm install && npm run dev     # http://localhost:3000
```

Extraction is scripted by default, so no API key is needed. To have Claude read the emails, set `ANTHROPIC_API_KEY`, then either set `SDRA_LIVE_EXTRACTION=1` for the screen, or run `python scripts/live_extraction_check.py --repeats 3` for real-model accuracy and pass^k.

## 9. Limitations

- **No real-model results yet.** The suite uses scripted extraction plus a noise model; the live check exists but hasn't been run.
- **Synthetic data and the agent's own cost model.** No claim of real savings; regret is measured inside the model.
- **One fix per option, one shortage per incident.** No reallocate-plus-buy combinations.
- **Splitting an order line** repeats fixed and per-day penalties on each part.
- **No consent records,** so every substitution that needs consent escalates.
- **No sign-in;** the signer is chosen on screen in the demo.
- **A crash between two saves** can stop a run, needing a person. It can't double-execute.
- **No negotiation in V1.** Standing quotes are taken as given.
- **Assumptions to validate in a pilot:** email is the main channel, ERP data is accurate, backups have standing quotes, supplier dates are delivery at the plant, late quantities can be cancelled without charge.

## 10. Repository map and roadmap

```
docs/          evaluation.md (generated results)
packs/         riyadh_ac, pumps_eu (seed.yaml + policy.yaml); specs/ for generated packs
scenarios/     36 scenarios + README (format, cost conventions, catalogue); specs/ for generated ones
src/sdra/      intake/ (read, verify), core/ (pegging, options, costs), agent/ (workflow, gateway,
               leak check, proposals), api/, evaluation.py, generator.py, schema.py, policy.py
tests/  scripts/  spikes/  ui/
```

**Next.**
- **V2: negotiation with backup suppliers.** A deterministic concession strategy chooses every offer; the model only writes and reads messages, inside a subgraph that never sees costs, penalties or the ceiling; every deal is validated; simulated suppliers run on a different model provider.
- **V1.1:** working calendar, templates in the pack, combined fixes, atomic saves, consent records, and real-model pass^k with a cheaper-model comparison.

## 11. References

- Long et al., *AIM-Bench*, arXiv 2508.11416 (2025)
- Liang & Xu, *When LLM agents negotiate*, arXiv 2608.07538 (2026)
- Debenedetti et al., *Defeating prompt injections by design (CaMeL)*, arXiv 2503.18813 (2025)
- Parasuraman, Sheridan & Wickens, *Types and levels of human interaction with automation*, IEEE SMC-A 30(3), 2000
- Buçinca, Malaya & Gajos, *To trust or to think*, CSCW 2021
- Vasconcelos et al., *Explanations can reduce overreliance on AI systems*, PACM HCI 7, 2023
- Yao et al., *τ-bench*, ICLR 2025
- Cemri et al., *Why do multi-agent LLM systems fail?*, NeurIPS 2025
- Edwards et al., *Agentic AI in engineering and manufacturing*, arXiv 2604.09633 (2026)
- Faratin, Sierra & Jennings, *Negotiation decision functions for autonomous agents*, 1998

---

**Author:** Mahmoud Ebied. Released under the [MIT License](LICENSE).
