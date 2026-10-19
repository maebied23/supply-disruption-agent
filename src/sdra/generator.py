"""Company-pack and scenario generator: records and messages from one source of truth.

Two inputs, both compact YAML written by a person:

  packs/specs/<name>.yaml      -> packs/<name>/seed.yaml
      products with BOMs, components with their suppliers, customers, orders; ids, sales
      order lines and work orders are derived consistently (a work order starts
      `assembly_days + buffer_days` before the promise date unless given).

  scenarios/specs/Sxx_*.yaml   -> scenarios/Sxx_*.yaml
      a scenario in the normal format, except that an email may give `compose:` (the facts)
      instead of `subject` and `body`. The generator writes the email text from those facts,
      in the requested language and phrasing, and derives `expected.claims` from the same
      facts, so the message and its labels cannot disagree. Everything else (the hand-worked
      expectations) passes through untouched.

Generation is deterministic: phrasing and date styles are chosen from the scenario id.
`scripts/generate.py` writes the files; a test fails if they are out of date.
"""

from __future__ import annotations

import hashlib
import textwrap
from datetime import date, timedelta
from typing import Any

AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر",
             "ديسمبر"]


def _pick(key: str, n: int) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % n


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def fmt_date(d: date, style: str, lang: str = "en") -> str:
    if lang == "ar":
        text = f"{d.day} {AR_MONTHS[d.month - 1]}" + (f" {d.year}" if style == "full" else "")
        return text.translate(AR_DIGITS) if style != "latin" else text
    match style:
        case "iso":
            return d.isoformat()
        case "month_first":
            suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
            return f"{EN_MONTHS[d.month - 1]} {d.day}{suffix}"
        case "full":
            return f"{d.day} {EN_MONTHS[d.month - 1]} {d.year}"
        case "numeric":
            return f"{d.day:02d}/{d.month:02d}/{d.year}"
    return f"{d.day} {EN_MONTHS[d.month - 1]}"


# --- messages --------------------------------------------------------------------------------

EN_CAUSES = ["a production line stoppage at our plant", "a shortage of raw material", "a port congestion delay",
             "an unplanned maintenance shutdown"]
AR_CAUSES = ["توقف خط الإنتاج في مصنعنا", "نقص في المواد الخام", "ازدحام في الميناء"]

EN_OPEN = ["Dear Procurement Team,", "Hello,", "Dear Sir or Madam,"]
EN_CLOSE = ["Best regards,\nExport Sales", "Kind regards,\nCustomer Service", "Regards,\nOrder Desk"]
AR_OPEN = ["السلام عليكم،", "تحية طيبة وبعد،"]
AR_CLOSE = ["مع التحية", "وتفضلوا بقبول فائق الاحترام"]


def _en(fact: dict, ds: str, key: str) -> str:
    po, item = fact["po"], fact["item"]
    cause = EN_CAUSES[_pick(key + "c", len(EN_CAUSES))]
    match fact["kind"]:
        case "split":  # partial_quantity on the original date + delay of the rest
            on, rest = fact["on_time_qty"], fact["late_qty"]
            orig, new = fmt_date(_d(fact["original_date"]), ds), fmt_date(_d(fact["new_date"]), ds)
            return [
                f"Due to {cause}, we can only deliver {on} of the {on + rest} units of {item} on {po} on {orig} "
                f"as confirmed. The remaining {rest} units will follow on {new}.",
                f"Regarding {po} ({item}): {on} units will ship for {orig} as planned. Because of {cause}, "
                f"the other {rest} units are now scheduled for {new}.",
            ][_pick(key, 2)]
        case "delay":
            new = fmt_date(_d(fact["new_date"]), ds)
            qty = fact.get("qty")
            what = f"the {qty} units of {item}" if qty else f"all units of {item}"
            return [
                f"Due to {cause}, {what} on {po} will now be delivered on {new}.",
                f"Please note that {po} is delayed: {what} are now expected on {new}.",
            ][_pick(key, 2)]
        case "confirmation":
            when = fmt_date(_d(fact["date"]), ds)
            return [
                f"We confirm {po}: {fact['qty']} units of {item} for delivery on {when}.",
                f"This is to confirm {po} for {fact['qty']} units of {item}, delivery {when}.",
            ][_pick(key, 2)]
        case "vague":
            return [f"There may be some delay on {po}. We will come back to you with details shortly.",
                    f"We are reviewing the schedule for {po} and might need more time. More news soon."][_pick(key, 2)]
        case "relative":  # a date the supplier only gives relative to something else
            return (f"For {po}, {fact['on_time_qty']} units of {item} ship as planned and the remaining "
                    f"{fact['late_qty']} units follow {fact['relative']}.")
    raise ValueError(fact["kind"])


def _ar(fact: dict, ds: str, key: str) -> str:
    po, item = fact["po"], fact["item"]
    cause = AR_CAUSES[_pick(key + "c", len(AR_CAUSES))]

    def n(x) -> str:
        return str(x).translate(AR_DIGITS) if ds != "latin" else str(x)

    match fact["kind"]:
        case "split":
            on, rest = fact["on_time_qty"], fact["late_qty"]
            orig, new = fmt_date(_d(fact["original_date"]), ds, "ar"), fmt_date(_d(fact["new_date"]), ds, "ar")
            return (f"بسبب {cause}، سنتمكن من توريد {n(on)} وحدة فقط من أصل {n(on + rest)} من الصنف {item} "
                    f"على طلب الشراء {po} في {orig} كما هو مؤكد، وسيتم توريد الكمية المتبقية {n(rest)} وحدة في {new}.")
        case "delay":
            new = fmt_date(_d(fact["new_date"]), ds, "ar")
            qty = fact.get("qty")
            what = f"{n(qty)} وحدة من الصنف {item}" if qty else f"جميع وحدات الصنف {item}"
            return f"نود إعلامكم بأنه بسبب {cause} سيتم توريد {what} على طلب الشراء {po} في {new}."
        case "confirmation":
            when = fmt_date(_d(fact["date"]), ds, "ar")
            return f"نؤكد طلب الشراء {po}: {n(fact['qty'])} وحدة من الصنف {item} للتسليم في {when}."
        case "vague":
            return f"بخصوص طلب الشراء {po}، قد يكون هناك تأخير بسيط. سنوافيكم بالتفاصيل قريباً."
    raise ValueError(fact["kind"])


def compose_email(sid: str, index: int, compose: dict) -> tuple[str, str]:
    """(subject, body) for an email given as facts."""
    key = f"{sid}:{index}"
    lang = compose.get("language", "en")
    facts = compose["facts"]
    styles = ["day_month", "month_first", "full", "iso"] if lang == "en" else ["arabic", "full"]
    ds = compose.get("date_style") or styles[_pick(key + "d", len(styles))]
    pos = sorted({f["po"] for f in facts})
    if lang == "ar":
        opening, closing = AR_OPEN[_pick(key + "o", len(AR_OPEN))], AR_CLOSE[_pick(key + "e", len(AR_CLOSE))]
        sentences = [_ar(f, ds, f"{key}:{i}") for i, f in enumerate(facts)]
        subject = compose.get("subject") or f"بخصوص {', '.join(pos)}"
    else:
        opening, closing = EN_OPEN[_pick(key + "o", len(EN_OPEN))], EN_CLOSE[_pick(key + "e", len(EN_CLOSE))]
        sentences = [_en(f, ds, f"{key}:{i}") for i, f in enumerate(facts)]
        subject = compose.get("subject") or f"{', '.join(pos)} delivery update"
    parts = [opening, *(textwrap.fill(x, 88) for x in sentences),
             *([compose["extra"].strip()] if compose.get("extra") else []), closing]
    return subject, "\n\n".join(parts) + "\n"


HELD = "other claims on this line need checking"


def claims_for(fact: dict, store_lines: dict[str, str], source: str | None) -> list[dict]:
    """Expected claims (scenario format) for one composed fact. `store_lines` maps a PO id to
    the PO line the claim resolves to. Optional fact keys: `verification`, `note`,
    `confidence` (what the extractor reports), `po_line` (override the resolved line), and
    `misread` + `misread_claim` (the extractor gets one value wrong; grounding must hold it,
    and the message's other claims on that line are held with it)."""
    if "misread" in fact:
        clean = {k: x for k, x in fact.items() if k not in ("misread", "misread_claim")}
        out = []
        for c in claims_for(clean, store_lines, source):
            if c["kind"] == fact["misread_claim"]:
                wrong = {k: (_d(x) if k == "new_date" else x) for k, x in fact["misread"].items()}
                out.append(c | wrong | {"verification": "unverified", "note": "not in the quoted text"})
            else:
                out.append(c | {"verification": "unverified", "note": HELD})
        return out
    line = fact.get("po_line", store_lines.get(fact["po"]))
    base = {"po_line_id": line, "item_id": fact["item"]} | ({"source": source} if source else {})
    if "confidence" in fact:
        base["confidence"] = fact["confidence"]
    if fact.get("duplicate"):  # restates what an earlier message said; changes nothing
        base["duplicate"] = True
    v = fact.get("verification", "verified")
    note = {"note": fact["note"]} if "note" in fact else {}
    match fact["kind"]:
        case "split":
            return [base | {"kind": "partial_quantity", "new_qty": fact["on_time_qty"], "verification": v} | note,
                    base | {"kind": "delay", "new_qty": fact["late_qty"], "new_date": _d(fact["new_date"]),
                            "verification": v} | note]
        case "delay":
            return [base | {"kind": "delay", "new_qty": fact.get("qty"), "new_date": _d(fact["new_date"]),
                            "verification": v} | note]
        case "confirmation":
            return [base | {"kind": "confirmation", "new_qty": fact["qty"], "new_date": _d(fact["date"]),
                            "verification": v} | note]
        case "vague":
            return [base | {"kind": "delay", "new_qty": None, "new_date": None, "verification": "unverified"}]
        case "relative":  # the extractor leaves the date empty; the whole line is held
            return [base | {"kind": "partial_quantity", "new_qty": fact["on_time_qty"], "verification": "unverified",
                            "note": HELD},
                    base | {"kind": "delay", "new_qty": fact["late_qty"], "new_date": None,
                            "verification": "unverified", "note": "missing new_date"}]
    raise ValueError(fact["kind"])


def expand_scenario(spec: dict, po_lines: dict[str, str]) -> dict:
    """Spec -> full scenario. `po_lines` maps PO ids to their line ids in the pack."""
    out = dict(spec)
    events, generated = [], []
    for i, e in enumerate(spec["events"]):
        e = dict(e)
        if e["type"] == "email" and "compose" in e["message"]:
            msg = dict(e["message"])
            compose = msg.pop("compose")
            msg["subject"], msg["body"] = compose_email(spec["id"], i, compose)
            msg.setdefault("received_at", e["at"])
            msg.setdefault("channel", "email")
            e["message"] = msg
            lines = {**po_lines, **compose.get("po_lines", {})}
            for f in compose["facts"]:
                generated += claims_for(f, lines, msg["id"])
        events.append(e)
    out["events"] = events
    expected = dict(spec["expected"])
    expected["claims"] = generated + list(expected.get("claims_extra", []))
    expected.pop("claims_extra", None)
    out["expected"] = {"claims": expected.pop("claims"), **expected}
    return out


# --- packs ----------------------------------------------------------------------------------


def build_seed(spec: dict) -> dict[str, Any]:
    """Compact company description -> seed.yaml rows (validated later by sdra.pack)."""
    loc = spec["location"]
    seed: dict[str, list] = {k: [] for k in (
        "locations", "items", "bom", "substitutions", "stock", "allocations", "customers", "sales_orders",
        "sales_order_lines", "work_orders", "suppliers", "supplier_items", "purchase_orders",
        "purchase_order_lines", "offers", "approvers")}
    seed["locations"].append({"id": loc["id"], "name": loc["name"]})
    for p in spec["products"]:
        seed["items"].append({"id": p["id"], "name": p["name"], "sourcing": "make", "critical": p.get("critical", True),
                              "assembly_lead_days": p["assembly_days"]})
        seed["bom"] += [{"parent_item_id": p["id"], "component_item_id": c, "qty_per": q} for c, q in p["bom"].items()]
        seed["stock"].append({"item_id": p["id"], "location_id": loc["id"], "on_hand": p.get("stock", 0)})
    for c in spec["components"]:
        seed["items"].append({"id": c["id"], "name": c["name"], "sourcing": "buy", "critical": c.get("critical", False)})
        seed["stock"].append({"item_id": c["id"], "location_id": loc["id"], "on_hand": c.get("stock", 0)})
        for s in c.get("sources", []):
            row = {"supplier_id": s["supplier"], "item_id": c["id"], "role": s["role"], "lead_time_days": s["lead_days"]}
            if "moq" in s:
                row["min_order_qty"] = s["moq"]
            if "price" in s:
                row["contract_price"] = s["price"]
            seed["supplier_items"].append(row)
    seed["substitutions"] = list(spec.get("substitutions", []))
    for s in spec["suppliers"]:
        seed["suppliers"].append({k: v for k, v in s.items()})
    for c in spec["customers"]:
        seed["customers"].append({k: v for k, v in c.items()})
    buffer = spec.get("buffer_days", 2)
    items = {p["id"]: p for p in spec["products"]}
    for n, o in enumerate(spec["orders"], start=1):
        so, line = o["so"], f"{o['so']}-1"
        seed["sales_orders"].append({"id": so, "customer_id": o["customer"], "order_date": o["ordered"]})
        promised = _d(o["promised"])
        seed["sales_order_lines"].append({
            "id": line, "sales_order_id": so, "item_id": o["product"], "qty": o["qty"], "unit_price": o["price"],
            "requested_date": o.get("requested", promised), "promised_date": promised,
            "partial_delivery_allowed": o.get("partial", False), **({"penalty": o["penalty"]} if "penalty" in o else {})})
        assembly = items[o["product"]]["assembly_days"]
        due = _d(o["due"]) if "due" in o else promised - timedelta(days=buffer)
        start = _d(o["start"]) if "start" in o else due - timedelta(days=assembly)
        seed["work_orders"].append({"id": o.get("wo", f"WO-{n}"), "item_id": o["product"], "qty": o.get("build_qty", o["qty"]),
                                    "sales_order_line_id": line, "start_date": start, "due_date": due,
                                    "status": "planned"})
    for po in spec["purchase_orders"]:
        seed["purchase_orders"].append({"id": po["id"], "supplier_id": po["supplier"], "order_date": po["ordered"]})
        seed["purchase_order_lines"].append({
            "id": po["id"].replace("PO-", "POL-", 1) + "-1", "purchase_order_id": po["id"], "item_id": po["item"],
            "qty_ordered": po["qty"], "unit_price": po["price"], "confirmed_date": po.get("confirmed", po["expected"]),
            "expected_date": po["expected"]})
    seed["offers"] = list(spec.get("offers", []))
    seed["approvers"] = list(spec["approvers"])
    return seed


def po_line_map(seed: dict) -> dict[str, str]:
    """PO id -> its (first) line id, for resolving composed facts."""
    out: dict[str, str] = {}
    for line in seed["purchase_order_lines"]:
        out.setdefault(line["purchase_order_id"], line["id"])
    return out
