"use client";

import { useCallback, useEffect, useState } from "react";
import { RequestView } from "@/components/RequestView";
import { api, Approver, AuditEntry, Scenario, setTimeZone, State, when } from "@/lib/api";

type Tab = "request" | "outbox" | "tasks" | "audit";

export default function Home() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [state, setState] = useState<State | null>(null);
  const [approvers, setApprovers] = useState<Approver[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("request");
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [session, setSession] = useState(0); // request ids restart in every session

  const apply = useCallback((s: State) => {
    setTimeZone(s.timezone);
    setState(s);
    setSelected((cur) => (s.pending.some((p) => p.id === cur) ? cur : s.pending[0]?.id ?? cur));
  }, []);

  const run = useCallback(
    async (f: () => Promise<State>) => {
      setError(null);
      try {
        apply(await f());
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [apply],
  );

  useEffect(() => {
    api.scenarios().then(setScenarios, (e) => setError(`API not reachable: ${e.message}`));
    api.state().then(apply, () => undefined);
  }, [apply]);

  useEffect(() => {
    if (state) api.approvers().then(setApprovers, () => undefined);
  }, [state?.scenario.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (tab === "audit" && state) api.audit().then((a) => setAudit(a.entries), () => undefined);
  }, [tab, state]);

  return (
    <div className="layout">
      <aside>
        <h1>Supply disruption approvals</h1>
        <label className="block small">
          Scenario
          <select value={state?.scenario.id ?? ""} onChange={(e) => {
            if (e.target.value) {
              setSession((n) => n + 1);
              run(() => api.start(e.target.value));
            }
          }}>
            <option value="">Choose a scenario…</option>
            {scenarios.map((s) => <option key={s.id} value={s.id}>{s.id} · {s.title}</option>)}
          </select>
        </label>
        {state && (
          <>
            <div className="clock">
              <div className="small muted">Simulated time</div>
              <div className="now">{when(state.now)}</div>
              <div className="small muted">
                {state.finished ? `Scenario window ends ${when(state.scenario.end)}` : `Next event ${when(state.next_event_at)}`}
              </div>
              <button disabled={state.finished} onClick={() => run(api.advance)}>Advance the clock</button>
            </div>

            <h3>Waiting for a decision</h3>
            {state.pending.length === 0 && <p className="muted small">Nothing waiting.</p>}
            <ul className="list">
              {state.pending.map((p) => (
                <li key={p.id}>
                  <button className={`item ${selected === p.id && tab === "request" ? "active" : ""}`} onClick={() => { setSelected(p.id); setTab("request"); }}>
                    <strong>{p.id}</strong> · {p.tier}
                    <br /><span className="small">{p.summary}</span>
                    <br /><span className="small muted">needs {p.outstanding_roles.join(", ").replace(/_/g, " ")}</span>
                  </button>
                </li>
              ))}
            </ul>

            <h3>History</h3>
            <ul className="list">
              {state.requests.filter((r) => !state.pending.some((p) => p.id === r.id)).map((r) => (
                <li key={r.id}>
                  <button className={`item ${selected === r.id && tab === "request" ? "active" : ""}`} onClick={() => { setSelected(r.id); setTab("request"); }}>
                    {r.id} · {r.option_id} · <span className={`badge ${r.decided}`}>{r.decided}</span>
                  </button>
                </li>
              ))}
            </ul>

            <div className="metrics small">
              <div>Decisions {state.metrics.decisions} · rejections {state.metrics.rejections}</div>
              <div>Override rate {state.metrics.override_rate === null ? "-" : `${Math.round(state.metrics.override_rate * 100)}%`}</div>
              <div>Audit log {state.audit.entries} entries · {state.audit.problems.length ? <span className="error">chain broken</span> : "chain intact"}</div>
            </div>
          </>
        )}
      </aside>

      <main>
        {error && <div className="panel error">{error}</div>}
        {!state && !error && <div className="panel muted">Choose a scenario to start. The backend runs on port 8000.</div>}
        {state && (
          <>
            <nav className="tabs">
              {(["request", "outbox", "tasks", "audit"] as Tab[]).map((t) => (
                <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
                  {{ request: "Decision", outbox: `Outbox (${state.outbox.length})`, tasks: `Review tasks (${state.review_tasks.length})`, audit: "Audit log" }[t]}
                </button>
              ))}
            </nav>
            {tab === "request" && (selected
              ? <RequestView key={`${session}:${selected}`} id={selected} approvers={approvers} onDecided={() => run(api.state)} />
              : <div className="panel muted">No approval requests yet. {state.finished ? "The scenario window has ended." : "Advance the clock."}</div>)}
            {tab === "outbox" && (
              <div className="panel">
                <p className="muted small">Messages are written to this outbox only; nothing is emailed.</p>
                {state.outbox.length === 0 && <p className="muted">Nothing sent.</p>}
                {state.outbox.map((m) => (
                  <details key={m.id} className="out">
                    <summary>{when(m.at)} · {m.id} → {m.to} · {m.subject}</summary>
                    <pre>{m.body}</pre>
                  </details>
                ))}
              </div>
            )}
            {tab === "tasks" && (
              <div className="panel">
                {state.review_tasks.length === 0 && <p className="muted">No review tasks.</p>}
                <ul>
                  {state.review_tasks.map((t) => (
                    <li key={t.id}><strong>{t.assignee_role.replace(/_/g, " ")}</strong> · {when(t.created_at)}<br />{t.reason}</li>
                  ))}
                </ul>
              </div>
            )}
            {tab === "audit" && (
              <div className="panel">
                <p className="small">{state.audit.problems.length ? state.audit.problems.join("; ") : "Hash chain verified: no entry was edited, removed or reordered."}</p>
                <table className="audit">
                  <thead><tr><th>#</th><th>Time</th><th>Actor</th><th>Event</th><th>Refs</th><th>Hash</th></tr></thead>
                  <tbody>
                    {audit.map((e) => (
                      <tr key={e.seq}>
                        <td>{e.seq}</td><td>{when(e.at)}</td><td>{e.actor}</td><td>{e.event}</td>
                        <td className="small">{e.ref_ids.join(", ")}</td><td className="mono small">{e.hash.slice(0, 10)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
