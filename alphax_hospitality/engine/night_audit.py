"""
AlphaX Hospitality — night audit.

The heartbeat of a PMS. Once per property per day it:

  1. refuses to run twice for the same business date (idempotence first —
     a double-run double-charges every in-house guest, which is the worst
     bug this system can have)
  2. posts one room charge + tax + levy per occupied room for the night
  3. processes no-shows per the cancellation policy and releases their
     inventory
  4. flags due-outs that never departed (overstays)
  5. reconciles the inventory ledger against reservations and logs drift
  6. raises interim invoices for long stays crossing a month end
  7. advances the property business date by one day

Everything keys off `business_date`, never off the server clock. A hotel
"day" runs to about 03:00; a charge posted at 01:30 belongs to yesterday.
Systems that use `nowdate()` here produce revenue that lands on the wrong
day and a P&L that never ties to the occupancy report.
"""

from __future__ import annotations

import json
import traceback

import frappe
from frappe import _
from frappe.utils import (add_days, add_months, cint, flt, get_first_day,
                          getdate, now_datetime, nowdate)

from . import availability, folio as folio_engine


def run_all():
    """Scheduler entry point. Each property audits independently — one
    failing property must not stop the rest."""
    props = frappe.get_all("Hotel Property", filters={"is_active": 1}, pluck="name")
    results = {}
    for p in props:
        try:
            results[p] = run(p)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Night audit failed: {p}")
            results[p] = {"ok": False, "error": "see error log"}
    return results


@frappe.whitelist()
def run(property_name: str, force: int = 0) -> dict:
    prop = frappe.get_doc("Hotel Property", property_name)
    business_date = getdate(prop.business_date or nowdate())

    # --- idempotence guard ---------------------------------------------
    already = frappe.db.exists("Night Audit Log", {
        "property": property_name,
        "business_date": business_date,
        "status": "Completed",
    })
    if already and not cint(force):
        return {"ok": False, "reason": "already_run", "log": already,
                "business_date": str(business_date)}

    log = frappe.get_doc({
        "doctype": "Night Audit Log",
        "property": property_name,
        "business_date": business_date,
        "status": "Running",
        "started_at": now_datetime(),
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    lines: list[str] = []
    stats = dict(rooms_charged=0, room_revenue=0.0, tax_posted=0.0,
                 no_shows=0, overstays=0, invoices=0)

    try:
        lines.append(f"=== Night audit {property_name} for {business_date} ===")

        stats.update(_post_room_charges(prop, business_date, lines))
        stats["no_shows"] = _process_no_shows(prop, business_date, lines)
        stats["overstays"] = _flag_overstays(prop, business_date, lines)
        _release_expired_allotments(prop, business_date, lines)

        drift = availability.reconcile(property_name, business_date,
                                       add_days(business_date, 365))
        if drift:
            lines.append(f"!! inventory drift corrected on {len(drift)} squares")
            for d in drift[:20]:
                lines.append(f"   {d['room_type']} {d['date']}: "
                             f"ledger {d['ledger_sold']} -> actual {d['actual_sold']}")
        else:
            lines.append("inventory ledger reconciled clean")

        stats["invoices"] = _month_end_interim(prop, business_date, lines)

        new_date = add_days(business_date, 1)
        prop.db_set("business_date", new_date, update_modified=False)
        prop.db_set("last_night_audit", now_datetime(), update_modified=False)
        lines.append(f"business date advanced {business_date} -> {new_date}")

        log.db_set("status", "Completed", update_modified=False)
        log.db_set("new_business_date", new_date, update_modified=False)
        log.db_set("finished_at", now_datetime(), update_modified=False)
        log.db_set("rooms_charged", stats["rooms_charged"], update_modified=False)
        log.db_set("room_revenue", stats["room_revenue"], update_modified=False)
        log.db_set("tax_posted", stats["tax_posted"], update_modified=False)
        log.db_set("no_shows", stats["no_shows"], update_modified=False)
        log.db_set("due_out_not_departed", stats["overstays"], update_modified=False)
        log.db_set("invoices_created", stats["invoices"], update_modified=False)
        log.db_set("log", "\n".join(lines), update_modified=False)
        frappe.db.commit()

        return {"ok": True, "log": log.name, "business_date": str(business_date),
                "new_business_date": str(new_date), **stats}

    except Exception:
        frappe.db.rollback()
        log.reload()
        log.db_set("status", "Failed", update_modified=False)
        log.db_set("finished_at", now_datetime(), update_modified=False)
        log.db_set("log", "\n".join(lines), update_modified=False)
        log.db_set("error", traceback.format_exc(), update_modified=False)
        frappe.db.commit()
        raise


# ---------------------------------------------------------------------------
# 1. room charges
# ---------------------------------------------------------------------------

def _post_room_charges(prop, business_date, lines) -> dict:
    """One charge per occupied room for tonight."""
    rows = frappe.db.sql("""
        SELECT r.name AS reservation, r.folio, rr.name AS room_row,
               rr.room, rr.room_type, rr.nightly_rate, rr.guest_name
          FROM `tabReservation Room` rr
          JOIN `tabReservation` r ON r.name = rr.parent
         WHERE r.property = %(prop)s
           AND r.docstatus = 1
           AND r.status = 'Checked In'
           AND rr.arrival_date  <= %(bd)s
           AND rr.departure_date >  %(bd)s
    """, {"prop": prop.name, "bd": business_date}, as_dict=True)

    vat_rate = _vat_rate(prop)
    levy_rate = flt(prop.municipality_fee_rate)
    charged = revenue = tax_total = 0.0

    for r in rows:
        if not r.folio:
            lines.append(f"!! reservation {r.reservation} is checked in with no folio — skipped")
            continue

        # Guard: never charge the same room twice for the same night.
        if _already_charged(r.folio, business_date, r.room_row):
            lines.append(f"   room {r.room} already charged for {business_date} — skipped")
            continue

        rate = flt(r.nightly_rate)
        levy = rate * levy_rate / 100 if levy_rate else 0.0
        vat = (rate + levy) * vat_rate / 100 if vat_rate else 0.0

        item = frappe.db.get_value("Room Type", r.room_type, "item_code")

        folio_engine.post(
            r.folio, txn_type="Room",
            description=_("Room {0} — {1}").format(r.room or r.room_type, business_date),
            charge=rate, item_code=item, qty=1, rate=rate,
            tax_amount=vat, business_date=business_date, source="Night Audit",
            reference_doctype="Reservation", reference_name=r.reservation)

        if levy:
            folio_engine.post(
                r.folio, txn_type="Levy",
                description=_("Municipality levy {0}%").format(levy_rate),
                charge=levy, qty=1, rate=levy,
                business_date=business_date, source="Night Audit",
                reference_doctype="Reservation", reference_name=r.reservation)

        charged += 1
        revenue += rate + levy
        tax_total += vat

    lines.append(f"posted {cint(charged)} room charges, revenue {revenue:.2f}, tax {tax_total:.2f}")
    return {"rooms_charged": cint(charged), "room_revenue": revenue, "tax_posted": tax_total}


def _already_charged(folio, business_date, room_row) -> bool:
    return bool(frappe.db.exists("Folio Transaction", {
        "parent": folio, "txn_type": "Room",
        "business_date": business_date, "voided": 0,
    }))


def _vat_rate(prop) -> float:
    if not prop.tax_template:
        return 0.0
    rows = frappe.get_all("Sales Taxes and Charges",
                          filters={"parent": prop.tax_template},
                          fields=["rate"], limit=1)
    return flt(rows[0].rate) if rows else 0.0


# ---------------------------------------------------------------------------
# 2. no-shows
# ---------------------------------------------------------------------------

def _process_no_shows(prop, business_date, lines) -> int:
    """A confirmed arrival that never checked in. Charge per policy,
    then give the inventory back — the room is sellable tomorrow."""
    res_list = frappe.get_all(
        "Reservation",
        filters={"property": prop.name, "docstatus": 1,
                 "status": ["in", ["Confirmed", "Tentative"]],
                 "arrival_date": business_date},
        fields=["name", "guest", "customer", "rate_plan"])

    count = 0
    for r in res_list:
        doc = frappe.get_doc("Reservation", r.name)
        policy = _policy_for(doc)
        charge = _no_show_amount(doc, policy)

        if charge > 0:
            f = folio_engine.open_folio(doc.name)
            folio_engine.post(
                f, txn_type="Misc",
                description=_("No-show charge — {0}").format(doc.name),
                charge=charge, qty=1, rate=charge,
                business_date=business_date, source="Night Audit",
                reference_doctype="Reservation", reference_name=doc.name)
            lines.append(f"no-show {doc.name}: charged {charge:.2f}")
        else:
            lines.append(f"no-show {doc.name}: no charge per policy")

        for rr in doc.rooms:
            availability.release(doc.property, rr.room_type,
                                 rr.arrival_date, rr.departure_date, 1)

        doc.db_set("status", "No Show", update_modified=False)
        count += 1

    if count:
        lines.append(f"processed {count} no-show(s)")
    return count


def _policy_for(res):
    if res.rate_plan:
        p = frappe.db.get_value("Rate Plan", res.rate_plan, "cancellation_policy")
        if p:
            return frappe.get_doc("Cancellation Policy", p)
    return None


def _no_show_amount(res, policy) -> float:
    if not policy:
        return flt(res.rooms[0].nightly_rate) if res.rooms else 0.0
    kind = policy.no_show_charge
    if kind == "None":
        return 0.0
    if kind == "First Night":
        return flt(res.rooms[0].nightly_rate) if res.rooms else 0.0
    if kind == "Full Stay":
        return flt(res.total_room_revenue)
    if kind == "Percent of Stay":
        return flt(res.total_room_revenue) * flt(policy.no_show_value) / 100
    if kind == "Fixed Amount":
        return flt(policy.no_show_value)
    return 0.0


# ---------------------------------------------------------------------------
# 3. overstays
# ---------------------------------------------------------------------------

def _flag_overstays(prop, business_date, lines) -> int:
    """Guests whose departure date has passed but who never checked out.
    We do NOT auto-extend — that hides a real operational problem. We
    flag, charge the night (already handled above if the room row still
    covers today), and let the front desk decide."""
    rows = frappe.db.sql("""
        SELECT r.name, rr.room, rr.departure_date
          FROM `tabReservation Room` rr
          JOIN `tabReservation` r ON r.name = rr.parent
         WHERE r.property = %(prop)s AND r.docstatus = 1
           AND r.status = 'Checked In'
           AND rr.departure_date <= %(bd)s
    """, {"prop": prop.name, "bd": business_date}, as_dict=True)

    for row in rows:
        lines.append(f"!! OVERSTAY room {row.room} res {row.name} "
                     f"due out {row.departure_date}")
        if row.room:
            frappe.db.set_value("Room", row.room, "status", "Occupied Dirty",
                                update_modified=False)
    if rows:
        lines.append(f"flagged {len(rows)} overstay(s) — front desk must resolve")
    return len(rows)


# ---------------------------------------------------------------------------
# 4. allotments
# ---------------------------------------------------------------------------

def _release_expired_allotments(prop, business_date, lines):
    """Corporate blocks that hit their release window return to general
    sale. Holding them past the cut-off is lost revenue."""
    contracts = frappe.get_all(
        "Corporate Contract",
        filters={"property": prop.name, "is_active": 1,
                 "allotment_rooms": [">", 0]},
        fields=["name", "allotment_rooms", "release_days", "rate_plan"])
    released = 0
    for c in contracts:
        cutoff = add_days(business_date, cint(c.release_days))
        rows = frappe.get_all(
            "Room Inventory",
            filters={"property": prop.name, "stay_date": ["<=", cutoff],
                     "blocked": [">", 0]},
            fields=["name", "blocked"])
        for r in rows:
            frappe.db.set_value("Room Inventory", r.name, "blocked", 0,
                                update_modified=False)
            availability._recompute_available(r.name)
            released += 1
    if released:
        lines.append(f"released {released} allotment night(s) back to general sale")


# ---------------------------------------------------------------------------
# 5. month-end interim invoices
# ---------------------------------------------------------------------------

def _month_end_interim(prop, business_date, lines) -> int:
    """A 40-night stay spanning a month boundary must not leave a month's
    revenue uninvoiced. On the last night of a month, raise an interim
    invoice for every in-house folio with a balance, then keep the folio
    open."""
    tomorrow = add_days(business_date, 1)
    if getdate(tomorrow) != getdate(get_first_day(tomorrow)):
        return 0

    folios = frappe.get_all(
        "Guest Folio",
        filters={"property": prop.name, "status": "Open",
                 "folio_type": ["in", ["Guest", "Master"]]},
        fields=["name", "balance"])

    made = 0
    for f in folios:
        if flt(f.balance) <= 0:
            continue
        try:
            inv = folio_engine.build_invoice(f.name, submit=True, is_interim=True)
            # Credit the folio so the balance zeroes without closing it.
            folio_engine.post(
                f.name, txn_type="Adjustment",
                description=_("Interim invoice {0}").format(inv),
                credit=flt(f.balance), business_date=business_date,
                source="Night Audit",
                reference_doctype="Sales Invoice", reference_name=inv)
            lines.append(f"interim invoice {inv} for folio {f.name} ({f.balance:.2f})")
            made += 1
        except Exception as e:
            lines.append(f"!! interim invoice failed for {f.name}: {e}")
    return made


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

@frappe.whitelist()
def rollback_last(property_name: str, reason: str):
    """Undo the last audit — the night manager ran it early by mistake.
    Reverses room charges for that date and steps the business date back.
    Deliberately manual and logged; never automatic."""
    log_name = frappe.db.get_value(
        "Night Audit Log",
        {"property": property_name, "status": "Completed"},
        "name", order_by="creation desc")
    if not log_name:
        frappe.throw(_("No completed audit to roll back"))

    log = frappe.get_doc("Night Audit Log", log_name)
    bd = getdate(log.business_date)

    txns = frappe.get_all(
        "Folio Transaction",
        filters={"business_date": bd, "source": "Night Audit", "voided": 0},
        fields=["name", "parent"])
    for t in txns:
        frappe.db.set_value("Folio Transaction", t.name, "voided", 1,
                            update_modified=False)
    for parent in {t.parent for t in txns}:
        folio_engine._recompute(frappe.get_doc("Guest Folio", parent), save=True)

    frappe.db.set_value("Hotel Property", property_name, "business_date", bd,
                        update_modified=False)
    log.db_set("status", "Rolled Back", update_modified=False)
    log.db_set("log", (log.log or "") +
               f"\n\nROLLED BACK by {frappe.session.user}: {reason}",
               update_modified=False)
    frappe.db.commit()
    return {"ok": True, "voided": len(txns), "business_date": str(bd)}
