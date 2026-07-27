"""
AlphaX Hospitality — front desk API.

Everything the front-desk SPA calls. Thin: validation and orchestration
only, with the real work in `engine/`.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate

from ..engine import availability, folio as folio_engine, rates


# ---------------------------------------------------------------------------
# the console
# ---------------------------------------------------------------------------

@frappe.whitelist()
def console(property_name: str, business_date=None):
    """Single call that fills the front-desk home screen: who is arriving,
    who is leaving, who is in house, and what the rooms look like."""
    bd = getdate(business_date or
                 frappe.db.get_value("Hotel Property", property_name, "business_date")
                 or nowdate())

    arrivals = frappe.get_all(
        "Reservation",
        filters={"property": property_name, "docstatus": 1,
                 "status": ["in", ["Confirmed", "Tentative"]],
                 "arrival_date": bd},
        fields=["name", "guest", "customer", "arrival_date", "departure_date",
                "adults", "children", "status", "grand_total", "deposit_paid",
                "source", "special_requests"],
        order_by="creation asc")

    departures = frappe.get_all(
        "Reservation",
        filters={"property": property_name, "docstatus": 1,
                 "status": "Checked In", "departure_date": bd},
        fields=["name", "guest", "folio", "departure_date"],
        order_by="creation asc")

    in_house = frappe.get_all(
        "Reservation",
        filters={"property": property_name, "docstatus": 1, "status": "Checked In"},
        fields=["name", "guest", "folio", "arrival_date", "departure_date"])

    rooms = frappe.get_all(
        "Room",
        filters={"property": property_name, "is_active": 1},
        fields=["name", "room_number", "room_type", "floor", "status"],
        order_by="floor asc, room_number asc")

    status_counts: dict[str, int] = {}
    for r in rooms:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    total_rooms = len(rooms)
    occupied = sum(v for k, v in status_counts.items() if k.startswith("Occupied"))

    return {
        "property": property_name,
        "business_date": str(bd),
        "arrivals": arrivals,
        "departures": departures,
        "in_house": in_house,
        "rooms": rooms,
        "room_status_counts": status_counts,
        "stats": {
            "total_rooms": total_rooms,
            "occupied": occupied,
            "occupancy_pct": round(occupied / total_rooms * 100, 1) if total_rooms else 0,
            "arrivals_due": len(arrivals),
            "departures_due": len(departures),
            "in_house": len(in_house),
            "vacant_dirty": status_counts.get("Vacant Dirty", 0),
            "out_of_order": status_counts.get("Out of Order", 0),
        },
    }


# ---------------------------------------------------------------------------
# quote and book
# ---------------------------------------------------------------------------

@frappe.whitelist()
def quote(property_name: str, room_type: str, arrival, departure,
          rate_plan: str | None = None, adults: int = 2, children: int = 0,
          rooms: int = 1):
    """Availability + price in one call — what the booking screen needs
    before it can show anything useful."""
    avail = availability.check_availability(property_name, room_type,
                                            arrival, departure, rooms)
    pricing = rates.resolve_stay_rate(property_name, room_type, rate_plan,
                                      arrival, departure, adults, children)
    return {"availability": avail, "pricing": pricing}


@frappe.whitelist()
def create_reservation(payload):
    """Create and confirm in one step, consuming inventory atomically.

    payload = {
      property, guest, customer, arrival_date, departure_date, rate_plan,
      adults, children, source, special_requests,
      rooms: [{ room_type, rooms: 1, nightly_rate? }]
    }
    """
    data = frappe.parse_json(payload) if isinstance(payload, str) else payload

    doc = frappe.new_doc("Reservation")
    doc.update({k: v for k, v in data.items() if k != "rooms"})
    doc.nights = frappe.utils.date_diff(data["departure_date"], data["arrival_date"])

    total = 0.0
    for spec in data.get("rooms", []):
        qty = cint(spec.get("rooms")) or 1
        priced = rates.resolve_stay_rate(
            data["property"], spec["room_type"], data.get("rate_plan"),
            data["arrival_date"], data["departure_date"],
            cint(data.get("adults")) or 2, cint(data.get("children")) or 0)
        nightly = flt(spec.get("nightly_rate")) or (
            priced["average_rate"] if priced["nights"] else 0)

        for _i in range(qty):
            doc.append("rooms", {
                "room_type": spec["room_type"],
                "room": spec.get("room"),
                "rate_plan": data.get("rate_plan"),
                "arrival_date": data["arrival_date"],
                "departure_date": data["departure_date"],
                "nights": doc.nights,
                "adults": cint(data.get("adults")) or 1,
                "children": cint(data.get("children")) or 0,
                "nightly_rate": nightly,
                "total_rate": nightly * doc.nights,
            })
            total += nightly * doc.nights

    doc.total_room_revenue = total
    doc.grand_total = total
    doc.status = data.get("status") or "Confirmed"
    doc.flags.ignore_permissions = False
    doc.insert()
    doc.submit()

    # Inventory consumption is inside the same transaction as the document.
    for spec in data.get("rooms", []):
        availability.consume(doc.property, spec["room_type"],
                             doc.arrival_date, doc.departure_date,
                             cint(spec.get("rooms")) or 1)

    frappe.db.commit()
    return {"ok": True, "reservation": doc.name, "total": total}


@frappe.whitelist()
def cancel_reservation(reservation: str, reason: str = ""):
    doc = frappe.get_doc("Reservation", reservation)
    doc.check_permission("write")
    if doc.status == "Checked In":
        frappe.throw(_("Check the guest out before cancelling"))

    for rr in doc.rooms:
        availability.release(doc.property, rr.room_type,
                             rr.arrival_date, rr.departure_date, 1)

    doc.db_set("status", "Cancelled", update_modified=False)
    doc.db_set("internal_notes",
               f"{doc.internal_notes or ''}\nCancelled by {frappe.session.user}: {reason}",
               update_modified=False)
    frappe.db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# check in / out
# ---------------------------------------------------------------------------

@frappe.whitelist()
def check_in(reservation: str, room_assignments=None):
    """room_assignments = { "<reservation room row name>": "<room>" }"""
    doc = frappe.get_doc("Reservation", reservation)
    doc.check_permission("write")

    if doc.status not in ("Confirmed", "Tentative"):
        frappe.throw(_("Reservation is {0} — cannot check in").format(doc.status))

    assigns = frappe.parse_json(room_assignments) if isinstance(room_assignments, str) \
        else (room_assignments or {})

    for rr in doc.rooms:
        room = assigns.get(rr.name) or rr.room
        if not room:
            room = _auto_assign(doc.property, rr.room_type)
        if not room:
            frappe.throw(_("No clean {0} room available to assign").format(rr.room_type))

        status = frappe.db.get_value("Room", room, "status")
        if status not in ("Vacant Clean", "Occupied Clean"):
            frappe.throw(_("Room {0} is {1} — not ready for arrival").format(room, status))

        frappe.db.set_value("Reservation Room", rr.name, "room", room,
                            update_modified=False)
        frappe.db.set_value("Room", room, "status", "Occupied Clean",
                            update_modified=False)

    folio = folio_engine.open_folio(doc.name)
    doc.db_set("status", "Checked In", update_modified=False)
    doc.db_set("actual_check_in", now_datetime(), update_modified=False)

    _register_shomoos(doc)
    frappe.db.commit()
    return {"ok": True, "folio": folio, "reservation": doc.name}


def _auto_assign(property_name, room_type):
    return frappe.db.get_value("Room", {
        "property": property_name, "room_type": room_type,
        "status": "Vacant Clean", "is_active": 1,
    }, "name", order_by="room_number asc")


def _register_shomoos(res):
    """Saudi properties must report guest arrivals to the Ministry of
    Interior. Queued, never blocking — a portal outage must not stop a
    check-in at the desk."""
    prop = frappe.get_cached_doc("Hotel Property", res.property)
    if not cint(prop.get("shomoos_enabled")):
        return
    frappe.enqueue(
        "alphax_hospitality.alphax_hospitality.integrations.shomoos.register_guest",
        queue="short", reservation=res.name, guest=res.guest,
        enqueue_after_commit=True)


@frappe.whitelist()
def check_out(reservation: str):
    doc = frappe.get_doc("Reservation", reservation)
    doc.check_permission("write")
    if not doc.folio:
        frappe.throw(_("No folio on this reservation"))
    return folio_engine.checkout(doc.folio)


# ---------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------

@frappe.whitelist()
def set_room_status(room: str, status: str, note: str = ""):
    frappe.db.set_value("Room", room, "status", status, update_modified=False)
    if status.startswith("Vacant Clean"):
        frappe.db.set_value("Room", room, {
            "last_cleaned": now_datetime(),
        }, update_modified=False)
    frappe.db.commit()
    return {"ok": True, "room": room, "status": status}


@frappe.whitelist()
def housekeeping_board(property_name: str, task_date=None):
    d = getdate(task_date or nowdate())
    tasks = frappe.get_all(
        "Housekeeping Task",
        filters={"property": property_name, "task_date": d},
        fields=["name", "room", "task_type", "status", "priority",
                "assigned_to", "started_at", "completed_at"],
        order_by="priority desc, room asc")
    rooms = frappe.get_all(
        "Room", filters={"property": property_name, "is_active": 1},
        fields=["name", "room_number", "room_type", "floor", "status"])
    return {"date": str(d), "tasks": tasks, "rooms": rooms}


# ---------------------------------------------------------------------------
# POS bridge — this is the differentiator
# ---------------------------------------------------------------------------

@frappe.whitelist()
def find_in_house(property_name: str, query: str):
    """Room number or guest name -> chargeable folios. Called by the POS
    'charge to room' flow."""
    q = f"%{query}%"
    rows = frappe.db.sql("""
        SELECT f.name AS folio, f.guest, f.balance, rr.room,
               g.guest_name, r.name AS reservation, r.departure_date
          FROM `tabGuest Folio` f
          JOIN `tabReservation` r ON r.name = f.reservation
          JOIN `tabReservation Room` rr ON rr.parent = r.name
          LEFT JOIN `tabGuest Profile` g ON g.name = f.guest
         WHERE f.property = %(prop)s
           AND f.status = 'Open'
           AND r.status = 'Checked In'
           AND (rr.room LIKE %(q)s OR g.guest_name LIKE %(q)s)
         LIMIT 20
    """, {"prop": property_name, "q": q}, as_dict=True)
    return rows


@frappe.whitelist()
def charge_to_room(folio: str, amount: float, description: str,
                   item_code: str | None = None, txn_type: str = "Food & Beverage",
                   pos_invoice: str | None = None):
    """Post a restaurant / bar / spa check straight onto the guest folio.

    The POS does NOT create a Sales Invoice in this path — the charge
    settles at checkout through the folio's invoice. That is the whole
    point: one invoice per stay, not one per coffee.
    """
    doc = frappe.get_doc("Guest Folio", folio)
    if doc.status != "Open":
        frappe.throw(_("Folio {0} is closed").format(folio))

    row = folio_engine.post(
        folio, txn_type=txn_type, description=description,
        charge=flt(amount), item_code=item_code, qty=1, rate=flt(amount),
        source="POS",
        reference_doctype="Sales Invoice" if pos_invoice else None,
        reference_name=pos_invoice)
    frappe.db.commit()

    doc.reload()
    return {"ok": True, "line": row, "new_balance": flt(doc.balance)}
