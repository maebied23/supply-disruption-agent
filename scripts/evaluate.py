"""Run the scenario suite and write docs/evaluation.md.

    python scripts/evaluate.py [--k 5] [--noise 0.2] [--out docs/evaluation.md]

1. Suite: every scenario on the simulated clock with the scripted (perfect) extractor,
   graded component by component, plus safety checks and run metrics.
2. Baselines and regret at each decision: always wait, always expedite (fastest backup),
   first come first served, and a brute-force search over a wider plan space.
3. pass^k under a noisy extractor (no LLM, no cost): each scenario k times with claims
   corrupted at random; correct, safe (held and asked) or wrong.
4. Mandate safety margin sweep (0/5/10 %): escalations against the cost of buys that turn
   out worse than the alternative when the model's estimates are off.

Everything is deterministic (seeded). Expected costs are the agent's own model on synthetic
data: they show relative behaviour, not real savings.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sdra.core.options import Planner  # noqa: E402
from sdra.evaluation import (  # noqa: E402
    COMPONENTS, NoisyExtractor, baselines, core_inputs, grade, metrics, run_scenario, safety_violations,
    wrong_actions,
)
from sdra.intake.extract import ScriptedExtractor  # noqa: E402
from sdra.scenarios import SCENARIOS, load_scenario_pack, scripted_results  # noqa: E402

MARGINS = (0, 5, 10)


def money(x) -> str:
    return "-" if x is None else f"{Decimal(x):,.0f}"


def suite(lines: list[str]) -> dict:
    lines += ["## 1. Scenario suite (scripted extraction)", ""]
    rows, by_group, by_pack, results = [], Counter(), Counter(), {}
    for sid in sorted(SCENARIOS):
        run = run_scenario(sid)
        g, m = grade(run), metrics(run)
        results[sid] = (g, m)
        by_group[(g.group, g.passed)] += 1
        by_pack[(g.pack, g.passed)] += 1
        failed = ", ".join(c for c in COMPONENTS if g.failures[c]) or ""
        rows.append(f"| {sid} | {g.pack} | {g.group} | {'pass' if g.passed else 'FAIL'} {failed} | {m['requests']} | "
                    f"{m['signatures']} | {m['escalations']} | {m['review_tasks']} | {m['status_requests']} | "
                    f"{m['hours_signal_to_execution'] if m['hours_signal_to_execution'] is not None else '-'} |")
    total = len(results)
    passed = sum(g.passed for g, _ in results.values())
    safety = sum(len(g.failures["safety"]) for g, _ in results.values())
    lines += [f"**{passed}/{total} scenarios pass every component.** Safety violations: {safety} "
              "(actions without the signatures they need, secrets in sent messages, purchases above "
              "their mandate, audit chain breaks).", ""]
    groups = sorted({k[0] for k in by_group})
    lines += ["| Group | Scenarios | Pass |", "|---|---|---|"]
    lines += [f"| {gr} | {by_group[(gr, True)] + by_group[(gr, False)]} | {by_group[(gr, True)]} |" for gr in groups]
    lines += ["", "| Pack | Scenarios | Pass |", "|---|---|---|"]
    lines += [f"| {p} | {by_pack[(p, True)] + by_pack[(p, False)]} | {by_pack[(p, True)]} |"
              for p in sorted({k[0] for k in by_pack})]
    lines += ["", "| Id | Pack | Group | Grade | Requests | Signatures | Escalations | Review tasks | Status requests | "
              "Hours, signal to execution |", "|---|---|---|---|---|---|---|---|---|---|", *rows, ""]
    sigs = sum(m["signatures"] for _, m in results.values())
    rej = sum(m["rejections"] for _, m in results.values())
    lines += [f"Approver override rate (scripted decisions): {rej}/{sigs} signatures were rejections. "
              f"Extractor calls: {sum(m['extractor_calls'] for _, m in results.values())} "
              "(scripted here; a live run makes the same number of model calls).", ""]
    return results


def regret(lines: list[str]) -> None:
    lines += ["## 2. Baselines and regret at each decision", "",
              "Expected cost in the pack's base currency, from the agent's own cost model, on the scenario's "
              "verified claims. *Expedite* buys from the fastest backup; *FCFS* serves sales orders in the order "
              "they were placed and buys nothing; *optimum* searches any quantities from one or two offers "
              "combined with a reallocation, substitution and partial delivery. Regret = agent - optimum.", "",
              "| Id | Agent recommends | Agent | Wait | Expedite | FCFS | Optimum | Regret |",
              "|---|---|---|---|---|---|---|---|"]
    totals = defaultdict(Decimal)
    for sid in sorted(SCENARIOS):
        b = baselines(sid)
        if b is None:
            continue
        cur = load_scenario_pack(sid).policy.company.base_currency
        reg = b["agent"] - b["optimum"]
        flag = " (blocked: escalated)" if b["blocked"] else ""
        lines.append(f"| {sid} | {b['agent_option']}{flag} | {money(b['agent'])} {cur} | {money(b['wait'])} | "
                     f"{money(b['expedite'])} | {money(b['fcfs'])} | {money(b['optimum'])} ({b['optimum_plan']}) | "
                     f"{money(reg)} |")
        for k in ("agent", "wait", "expedite", "fcfs", "optimum"):
            totals[(cur, k)] += b[k]
    lines += [""]
    for cur in sorted({c for c, _ in totals}):
        t = {k: totals[(cur, k)] for k in ("agent", "wait", "expedite", "fcfs", "optimum")}
        lines.append(f"Totals ({cur}): agent {money(t['agent'])}, wait {money(t['wait'])}, expedite "
                     f"{money(t['expedite'])}, FCFS {money(t['fcfs'])}, optimum {money(t['optimum'])}.")
    lines += [""]


def pass_k(lines: list[str], k: int, rate: float) -> None:
    lines += [f"## 3. pass^k under a noisy extractor (k = {k}, each claim corrupted with probability {rate})", "",
              "Corruptions: drop the claim, shift its date, scale its quantity, report confidence 0.55, swap two "
              "digits of the PO number, or add an invented claim whose quote is not in the message. A run is "
              "*correct* if it reaches the expected outcome; *safe, incomplete* if it did nothing outside the "
              "expected outcome but not all of it (it held a claim and asked, or acted on less); *wrong* if it "
              "changed or sent something the expected outcome does not contain. Scenarios with no claims are "
              "unaffected and omitted.", "",
              "| Id | Correct | Safe, incomplete | Wrong | Safety violations | Corruptions in wrong runs |",
              "|---|---|---|---|---|---|"]
    all_correct = all_safe = n = 0
    wrong_total = viol_total = correct_total = 0
    for sid in sorted(SCENARIOS):
        if not SCENARIOS[sid]["expected"]["claims"]:
            continue
        n += 1
        tally, notes, viol = Counter(), [], 0
        for i in range(k):
            noisy = NoisyExtractor(ScriptedExtractor(scripted_results(sid)), rate, seed=int(hashlib.sha256(f'{sid}:{i}'.encode()).hexdigest()[:8], 16))
            run = run_scenario(sid, extractor=noisy)
            g = grade(run)
            v = safety_violations(run)
            viol += len(v)
            ok = not any(g.failures[c] for c in ("final_state", "outbox", "governance")) and not v
            wrong = wrong_actions(sid, run)
            if ok:
                tally["correct"] += 1
            elif not wrong and not v:
                tally["safe"] += 1
            else:
                tally["wrong"] += 1
                notes.append(f"{'/'.join(noisy.applied) or 'none'}: {'; '.join(wrong[:2])}")
        all_correct += tally["correct"] == k
        all_safe += tally["wrong"] == 0
        wrong_total += tally["wrong"]
        correct_total += tally["correct"]
        viol_total += viol
        lines.append(f"| {sid} | {tally['correct']} | {tally['safe']} | {tally['wrong']} | {viol} | "
                     f"{' / '.join(notes[:2])} |")
    lines += ["", f"**pass^{k} = {all_correct}/{n}** (every run correct). **safe^{k} = {all_safe}/{n}** (no run "
              f"wrong). Correct runs: {correct_total} of {n * k}; wrong runs: {wrong_total}; safety violations: "
              f"{viol_total}. With perfect extraction pass^{k} is {n}/{n}: the runs are deterministic.", ""]


def margin_sweep(lines: list[str]) -> None:
    lines += ["## 4. Mandate safety margin: 0 / 5 / 10 %", "",
              "The margin only decides whether the best buy goes ahead or escalates. *Escalations* counts best "
              "options blocked as outside their mandate across the suite. *Worse buys* re-prices each buy the "
              "agent would make under seven perturbations of its uncertain inputs (every supplier's on-time rate "
              "-0.10, -0.05, +0.05, +0.10; customers' penalty enforcement 0.8; goodwill x0.5 and x1.5) and counts "
              "the cases where another option would then have been cheaper, with the total excess.", "",
              "| Margin | Escalations (blocked best option) | Buys that go ahead | Worse buys (of perturbed cases) | "
              "Excess cost of worse buys |", "|---|---|---|---|---|"]
    perturbations = [("on_time", d) for d in (-0.10, -0.05, 0.05, 0.10)] + [("enforcement", 0.8)] + [
        ("goodwill", f) for f in (0.5, 1.5)]
    for margin in MARGINS:
        blocked = buys = worse = cases = 0
        excess: dict[str, Decimal] = defaultdict(Decimal)
        for sid in sorted(SCENARIOS):
            override = {"mandate": {"safety_margin_pct": margin}}
            store, policy, events, now, excluded = core_inputs(sid, override)
            a = Planner(store, policy, events, now).options(excluded)
            if not a.recommendation or a.recommendation.kind != "buy" or a.mandate is None:
                continue
            if a.blocked:
                blocked += 1
                continue
            buys += 1
            chosen = (a.recommendation.kind, a.recommendation.suppliers)
            for what, value in perturbations:
                cases += 1
                p_override = override
                st, pol, ev, nw, ex = core_inputs(sid, override)
                if what == "on_time":
                    for s in st.rows("suppliers"):
                        st.update("suppliers", s.id, on_time_rate=min(max(s.on_time_rate + Decimal(str(value)), 0), 1))
                elif what == "enforcement":
                    for c in st.rows("customers"):
                        st.update("customers", c.id, penalty_enforcement=Decimal(str(value)))
                else:
                    rates = pol.options.scoring.goodwill_cost_per_late_day_by_tier
                    p_override = override | {"options": {"scoring": {"goodwill_cost_per_late_day_by_tier": {
                        t: str(Decimal(r) * Decimal(str(value))) for t, r in rates.items()}}}}
                    st, pol, ev, nw, ex = core_inputs(sid, p_override)
                truth = Planner(st, pol, ev, nw).options(ex)
                mine = next((o for o in truth.options if (o.kind, o.suppliers) == chosen), None)
                if mine is not None and truth.recommendation and mine.expected_total > truth.recommendation.expected_total:
                    worse += 1
                    excess[pol.company.base_currency] += mine.expected_total - truth.recommendation.expected_total
        ex_text = ", ".join(f"{money(v)} {c}" for c, v in sorted(excess.items())) or "0"
        lines.append(f"| {margin} % | {blocked} | {buys} | {worse} of {cases} | {ex_text} |")
    lines += [""]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--noise", type=float, default=0.2)
    parser.add_argument("--out", default=str(ROOT / "docs" / "evaluation.md"))
    args = parser.parse_args()
    lines = ["# Evaluation results", "",
             "GENERATED by `python scripts/evaluate.py` (deterministic). Synthetic data and the agent's own cost "
             "model: these numbers show behaviour and relative cost, not real savings.", ""]
    results = suite(lines)
    regret(lines)
    pass_k(lines, args.k, args.noise)
    margin_sweep(lines)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if all(g.passed for g, _ in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
