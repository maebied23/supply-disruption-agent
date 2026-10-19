"use client";

import { useEffect, useMemo, useState } from "react";
import { api, Approver, RequestDetail, Source, when } from "@/lib/api";

// The approval screen. Built to make checking cheap rather than to persuade: the supplier's
// own words with the extracted span marked, what changed since the analysis, the
// alternatives as differences from the recommendation, and what the numbers assume.

function Highlighted({ source }: { source: Source }) {
  const parts = useMemo(() => {
    const spans: [number, number][] = [];
    const flat = source.body.replace(/\s+/g, " ");
    for (const q of source.quotes) {
      const i = flat.toLowerCase().indexOf(q.replace(/\s+/g, " ").toLowerCase());
      if (i >= 0) spans.push([i, i + q.replace(/\s+/g, " ").length]);
    }
    spans.sort((a, b) => a[0] - b[0]);
    const merged: [number, number][] = []; // quotes of several claims may overlap
    for (const [a, b] of spans) {
      const last = merged[merged.length - 1];
      if (last && a <= last[1]) last[1] = Math.max(last[1], b);
      else merged.push([a, b]);
    }
    const out: { text: string; mark: boolean }[] = [];
    let pos = 0;
    for (const [a, b] of merged) {
      out.push({ text: flat.slice(pos, a), mark: false }, { text: flat.slice(a, b), mark: true });
      pos = b;
    }
    out.push({ text: flat.slice(pos), mark: false });
    return out;
  }, [source]);
  return <p className="quote">{parts.map((p, i) => (p.mark ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}</p>;
}

export function RequestView({ id, approvers, onDecided }: { id: string; approvers: Approver[]; onDecided: () => void }) {
  const [req, setReq] = useState<RequestDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [signer, setSigner] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [exclude, setExclude] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setReq(null);
    setRejecting(false);
    setExclude([]);
    setComment("");
    api.request(id).then(setReq, (e) => setError(String(e.message ?? e)));
  }, [id]);

  const eligible = useMemo(
    () => approvers.filter((a) => req && a.roles.some((r) => req.outstanding_roles.includes(r)) && !req.signatures.some((s) => s.approver_id === a.id)),
    [approvers, req],
  );
  useEffect(() => {
    if (!eligible.some((a) => a.id === signer)) setSigner(eligible[0]?.id ?? "");
  }, [eligible, signer]);

  if (error) return <div className="panel error">{error}</div>;
  if (!req) return <div className="panel muted">Loading {id}...</div>;

  const d = req.detail;
  const open = req.decided === null && req.outstanding_roles.length > 0;
  const cur = d.base_currency;
  const suppliers = Array.from(new Set(d.alternatives.find((a) => a.option_id === req.option_id)?.suppliers ?? []));

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      await api.decide(id, { approver_id: signer, decision, exclude_supplier_ids: decision === "reject" ? exclude : [], comment });
      setRejecting(false);
      setExclude([]);
      setComment("");
      setReq(await api.request(id)); // signatures, outstanding roles and outcome have moved on
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="request">
      <header className="panel">
        <div className="row spread">
          <h2>{d.summary}</h2>
          <span className={`tier tier-${req.tier}`}>{req.tier === "dual" ? "Two signatures" : "One signature"}</span>
        </div>
        <p className="muted">
          {req.id} · {req.incident_id} · requested {when(req.created_at)} · expires {when(req.expires_at)}
        </p>
        <div className="signers">
          {req.roles.map((role) => {
            const s = req.signatures.find((x) => x.role === role);
            return (
              <span key={role} className={`signer ${s ? (s.decision === "approve" ? "ok" : "bad") : ""}`}>
                {role.replace(/_/g, " ")}: {s ? `${s.decision}d by ${s.approver_id}` : "waiting"}
              </span>
            );
          })}
        </div>
        <p className="muted small">Why these signers: rules {req.rule_ids.join(", ")}</p>
      </header>

      {open ? (
        <section className={`panel ${req.changed_since_analysis.length ? "warn" : "calm"}`}>
          <h3>What changed since the analysis</h3>
          {req.changed_since_analysis.length === 0 ? (
            <p>Nothing. Stock, open orders, offers, promise dates and supplier messages are as they were at {when(req.created_at)}.</p>
          ) : (
            <ul>{req.changed_since_analysis.map((c) => <li key={c}>{c}</li>)}</ul>
          )}
        </section>
      ) : (
        <section className="panel calm">
          <h3>Outcome: {req.decided}</h3>
          <p>
            {req.decided === "approved" || req.decided === "executed"
              ? "Facts were re-checked before execution and had not changed; the actions below were carried out. See the outbox and the audit log."
              : "Nothing below was carried out."}
          </p>
          {req.void_reasons.length > 0 && <ul>{req.void_reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
        </section>
      )}

      <section className="panel">
        <h3>What the supplier said</h3>
        {d.sources.length === 0 && <p className="muted">No supplier message behind this plan (generated by the clock or records).</p>}
        {d.sources.map((s) => (
          <div key={s.message_id} className="source">
            <p className="muted small">
              {s.message_id} · from {s.sender} · {when(s.received_at)} · “{s.subject}”
            </p>
            <Highlighted source={s} />
            <table>
              <thead><tr><th>Read as</th><th>PO line</th><th>Qty</th><th>Date</th><th>Check</th></tr></thead>
              <tbody>
                {s.claims.map((c, i) => (
                  <tr key={i}>
                    <td>{c.kind.replace(/_/g, " ")}</td><td>{c.po_line_id ?? "-"}</td><td>{c.qty ?? "-"}</td><td>{c.date ?? "-"}</td>
                    <td><span className={`badge ${c.verification}`}>{c.verification}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        <p className="muted small">Highlighted text is the quote the extraction relied on; the system checked it appears word for word.</p>
      </section>

      <section className="panel">
        <h3>The shortage</h3>
        {d.shortage ? (
          <p>
            {d.shortage.short_qty} × {d.shortage.item_id} missing by {d.shortage.need_by} for {d.shortage.work_order_id}
            {d.shortage.customer ? ` (${d.shortage.customer}, ${d.shortage.so_line_id})` : ""}. {d.shortage.line_at_risk} units at risk;
            contractual penalty {d.shortage.penalty} {cur}.
          </p>
        ) : (
          <p>None: every order is still covered on time. This only brings the purchase-order records in line with what the supplier said.</p>
        )}
      </section>

      {d.alternatives.length > 0 && <section className="panel">
        <h3>Alternatives (expected cost, {cur})</h3>
        <table className="numbers">
          <thead>
            <tr><th>Option</th><th>Extra cost</th><th>Risk</th><th>Penalty</th><th>Goodwill</th><th>Total</th><th>vs recommended</th></tr>
          </thead>
          <tbody>
            {d.alternatives.map((a) => (
              <tr key={a.option_id} className={a.option_id === req.option_id ? "chosen" : ""}>
                <td><strong>{a.option_id}</strong><br /><span className="muted small">{a.description}</span></td>
                <td>{a.incremental}</td><td>{a.risk}</td><td>{a.penalty}</td><td>{a.goodwill}</td><td>{a.expected_total}</td>
                <td>{a.option_id === req.option_id ? "recommended" : `+${a.delta_vs_recommendation}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {d.excluded.length > 0 && (
          <ul className="muted small">{d.excluded.map((x) => <li key={x.option}>Not offered: {x.option} ({x.reason})</li>)}</ul>
        )}
        {d.mandate && (
          <p className="small">
            Price ceiling for this purchase: {d.mandate.max_unit_price} {cur}/unit (beyond it, {d.mandate.next_best} is better). Offer:{" "}
            {d.mandate.offer_unit_price} {cur}/unit. New commitment {d.commitment} {cur}.
          </p>
        )}
      </section>}

      <section className="panel">
        <h3>{d.shortage ? "What this assumes" : "Why"}</h3>
        <ul>{[...d.evidence, ...d.assumptions].map((a) => <li key={a}>{a}</li>)}</ul>
      </section>

      <section className="panel">
        <h3>What will happen if approved</h3>
        <ol>
          {d.actions.map((a) => (
            <li key={a.id}>
              {a.text} <span className="muted small">({a.tier}; {a.rules.join(", ") || "no rule"})</span>
              {a.message && (
                <details>
                  <summary className="small">Exact message to {a.message.to}</summary>
                  <pre>{a.message.subject}{"\n\n"}{a.message.body}</pre>
                </details>
              )}
            </li>
          ))}
        </ol>
        <p className="muted small">Nothing is sent or changed until every signature is in and the facts are re-checked.</p>
      </section>

      {open && (
        <section className="panel decision">
          <h3>Your decision</h3>
          {eligible.length === 0 ? (
            <p className="muted">No remaining approver holds an outstanding role.</p>
          ) : (
            <>
              <label>
                Signing as{" "}
                <select value={signer} onChange={(e) => setSigner(e.target.value)}>
                  {eligible.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.roles.join(", ")})</option>)}
                </select>
              </label>
              {rejecting && (
                <div className="reject">
                  {suppliers.length > 0 && (
                    <fieldset>
                      <legend className="small">Exclude from the re-plan (optional)</legend>
                      {suppliers.map((s) => (
                        <label key={s} className="check">
                          <input type="checkbox" checked={exclude.includes(s)}
                            onChange={(e) => setExclude(e.target.checked ? [...exclude, s] : exclude.filter((x) => x !== s))} />
                          {s}
                        </label>
                      ))}
                    </fieldset>
                  )}
                  <label className="block small">
                    Comment (recorded in the audit log; not read by the agent)
                    <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={2} />
                  </label>
                </div>
              )}
              <div className="row">
                {!rejecting ? (
                  <>
                    <button className="primary" disabled={busy || !signer} onClick={() => decide("approve")}>Approve</button>
                    <button disabled={busy} onClick={() => setRejecting(true)}>Reject…</button>
                  </>
                ) : (
                  <>
                    <button className="danger" disabled={busy || !signer} onClick={() => decide("reject")}>Confirm rejection</button>
                    <button disabled={busy} onClick={() => setRejecting(false)}>Back</button>
                  </>
                )}
              </div>
              {error && <p className="error">{error}</p>}
            </>
          )}
          <p className="muted small">Demo: no sign-in. A deployment takes the signer from the company login.</p>
        </section>
      )}
    </div>
  );
}
