// Types mirror src/sdra/api/app.py responses. All money values arrive as formatted strings.

export type Signature = {
  approver_id: string;
  role: string;
  decision: "approve" | "reject";
  exclude_supplier_ids: string[];
  comment: string;
  decided_at: string;
};

export type RequestSummary = {
  id: string;
  incident_id: string;
  option_id: string;
  summary: string;
  tier: "approve" | "dual" | "execute";
  roles: string[];
  outstanding_roles: string[];
  decided: string | null;
  created_at: string;
  expires_at: string;
  signatures: Signature[];
  void_reasons: string[];
};

export type Alternative = {
  option_id: string;
  description: string;
  suppliers: string[];
  incremental: string;
  risk: string;
  penalty: string;
  goodwill: string;
  expected_total: string;
  delta_vs_recommendation: string;
};

export type Source = {
  message_id: string;
  sender: string;
  received_at: string;
  subject: string;
  body: string;
  quotes: string[];
  claims: { kind: string; po_line_id: string | null; qty: number | null; date: string | null; verification: string; confidence: number }[];
};

export type RequestDetail = RequestSummary & {
  changed_since_analysis: string[];
  rule_ids: string[];
  evidence: string[];
  detail: {
    summary: string;
    shortage: { item_id: string; short_qty: number; need_by: string; work_order_id: string; so_line_id: string | null; customer: string | null; line_at_risk: number; penalty: string } | null;
    actions: { id: string; kind: string; text: string; tier: string; rules: string[]; message: { to: string; subject: string; body: string } | null }[];
    alternatives: Alternative[];
    excluded: { option: string; reason: string }[];
    mandate: { max_unit_price: string; currency: string; next_best: string; offer_unit_price: string; within: boolean } | null;
    commitment: string;
    sources: Source[];
    evidence: string[];
    assumptions: string[];
    base_currency: string;
  };
};

export type State = {
  scenario: { id: string; title: string; end: string };
  now: string;
  next_event_at: string | null;
  finished: boolean;
  base_currency: string;
  timezone: string;
  pending: RequestSummary[];
  requests: RequestSummary[];
  incidents: { id: string; status: string; replan_count: number; plans: number }[];
  outbox: { id: string; at: string; to: string; kind: string; template: string | null; subject: string; body: string }[];
  review_tasks: { id: string; assignee_role: string; reason: string; created_at: string; ref_ids: string[] }[];
  notifications: { at: string; request_id: string; roles: string[] }[];
  metrics: { requests: number; decisions: number; rejections: number; override_rate: number | null; by_outcome: Record<string, number> };
  audit: { entries: number; problems: string[] };
};

export type Approver = { id: string; name: string; roles: string[] };
export type Scenario = { id: string; title: string; group: string | null };
export type AuditEntry = { seq: number; at: string; actor: string; event: string; incident_id: string | null; ref_ids: string[]; after: Record<string, unknown> | null; hash: string };

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { cache: "no-store", headers: { "content-type": "application/json" }, ...init });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  scenarios: () => call<Scenario[]>("/scenarios"),
  start: (scenario_id: string) => call<State>("/session", { method: "POST", body: JSON.stringify({ scenario_id }) }),
  state: () => call<State>("/state"),
  advance: () => call<State>("/advance", { method: "POST" }),
  approvers: () => call<Approver[]>("/approvers"),
  request: (id: string) => call<RequestDetail>(`/requests/${id}`),
  decide: (id: string, body: { approver_id: string; decision: "approve" | "reject"; exclude_supplier_ids: string[]; comment: string }) =>
    call<State>(`/requests/${id}/decisions`, { method: "POST", body: JSON.stringify(body) }),
  audit: () => call<{ problems: string[]; entries: AuditEntry[] }>("/audit"),
};

// The company's time zone comes from the pack (policy.company.timezone) via /api/state.
let zone = "UTC";
export function setTimeZone(tz: string) {
  zone = tz;
}

export function when(iso: string | null): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("en-GB", { timeZone: zone, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
