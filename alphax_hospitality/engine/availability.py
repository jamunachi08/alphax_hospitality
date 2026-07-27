"""
AlphaX Hospitality — availability engine.

THE design decision in a PMS. Everything else can be refactored later;
this cannot, because reservations accumulate against it.

Two ways to answer "can I sell a Deluxe for 3 nights from the 14th":

  (a) Scan reservations, expand each into nights, subtract from room
      count. Correct, trivially simple, and O(reservations). It dies at
      roughly 200 rooms × 90-day windows, and it cannot be pushed to a
      channel manager because there is no materialised number to push.

  (b) Keep a bucket per (property, room_type, night) with total / sold /
      blocked / out-of-order, and mutate it transactionally on every
      booking. Availability becomes an indexed range read. This is what
      every real PMS does, and it is the exact ARI shape Booking.com,
      Agoda and Almosafer want.

We do (b). The buckets are the ledger; reservations are the documents.
They are reconciled nightly by the night audit so drift is detectable
rather than silent.

Concurrency: two agents selling the last room at the same instant is the
canonical PMS race. We take a row lock (`FOR UPDATE`) across the whole
date range inside one transaction before decrementing. On MariaDB with
the (property, room_type, stay_date) index this locks a handful of rows
for microseconds.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, getdate, nowdate


# ---------------------------------------------------------------------------
# bucket maintenance
# ---------------------------------------------------------------------------

def ensure_buckets(property_name: str, room_type: str,
                   from_date, to_date) -> None:
    """Create any missing inventory rows for the range, seeded from the
    physical room count. Cheap and idempotent; called before every read
    so a newly-opened date range just works."""
    from_date, to_date = getdate(from_date), getdate(to_date)
    if to_date < from_date:
        return

    existing = {
        getdate(r.stay_date)
        for r in frappe.get_all(
            "Room Inventory",
            filters={"property": property_name, "room_type": room_type,
                     "stay_date": ["between", [from_date, to_date]]},
            fields=["stay_date"])
    }

    total = frappe.db.count("Room", {
        "property": property_name, "room_type": room_type, "is_active": 1})

    missing = []
    d = from_date
    while d <= to_date:
        if d not in existing:
            missing.append(d)
        d = add_days(d, 1)

    if not missing:
        return

    for d in missing:
        ooo = _ooo_count(property_name, room_type, d)
        doc = frappe.get_doc({
            "doctype": "Room Inventory",
            "property": property_name,
            "room_type": room_type,
            "stay_date": d,
            "total_rooms": total,
            "sold": 0,
            "blocked": 0,
            "out_of_order": ooo,
            "available": max(0, total - ooo),
        })
        doc.insert(ignore_permissions=True)


def _ooo_count(property_name, room_type, d) -> int:
    return frappe.db.count("Room", {
        "property": property_name, "room_type": room_type,
        "ooo_from": ["<=", d], "ooo_to": [">=", d],
    })


def rebuild_totals(property_name: str, room_type: str | None = None,
                   from_date=None, to_date=None) -> int:
    """Re-seed `total_rooms` after rooms are added, removed or taken out
    of order. Does NOT touch `sold` — that belongs to reservations."""
    filters = {"property": property_name}
    if room_type:
        filters["room_type"] = room_type
    if from_date and to_date:
        filters["stay_date"] = ["between", [getdate(from_date), getdate(to_date)]]
    elif from_date:
        filters["stay_date"] = [">=", getdate(from_date)]

    rows = frappe.get_all("Room Inventory", filters=filters,
                          fields=["name", "room_type", "stay_date"])
    touched = 0
    for r in rows:
        total = frappe.db.count("Room", {
            "property": property_name, "room_type": r.room_type, "is_active": 1})
        ooo = _ooo_count(property_name, r.room_type, r.stay_date)
        frappe.db.set_value("Room Inventory", r.name, {
            "total_rooms": total, "out_of_order": ooo,
        }, update_modified=False)
        _recompute_available(r.name)
        touched += 1
    frappe.db.commit()
    return touched


def _recompute_available(name: str) -> int:
    row = frappe.db.get_value(
        "Room Inventory", name,
        ["total_rooms", "sold", "blocked", "out_of_order", "overbook_limit"],
        as_dict=True)
    avail = (cint(row.total_rooms) + cint(row.overbook_limit)
             - cint(row.sold) - cint(row.blocked) - cint(row.out_of_order))
    avail = max(0, avail)
    frappe.db.set_value("Room Inventory", name, "available", avail,
                        update_modified=False)
    return avail


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_availability(property_name: str, from_date, to_date,
                     room_type: str | None = None, include_rates: int = 0):
    """Grid for the tape chart and the booking screen.

    Note the date semantics: a stay from the 14th to the 17th consumes
    nights 14, 15, 16 — NOT 17. Departure day is not a night. Getting
    this wrong is the single most common hotel-software bug.
    """
    from_date, to_date = getdate(from_date), getdate(to_date)
    last_night = add_days(to_date, -1) if to_date > from_date else from_date

    types = ([room_type] if room_type else
             frappe.get_all("Room Type", filters={"property": property_name},
                            pluck="name"))
    for rt in types:
        ensure_buckets(property_name, rt, from_date, last_night)

    rows = frappe.get_all(
        "Room Inventory",
        filters={"property": property_name, "room_type": ["in", types],
                 "stay_date": ["between", [from_date, last_night]]},
        fields=["room_type", "stay_date", "total_rooms", "sold", "blocked",
                "out_of_order", "overbook_limit", "available",
                "stop_sell", "closed_to_arrival", "closed_to_departure",
                "min_stay_override"],
        order_by="stay_date asc")

    grid: dict[str, dict] = {}
    for r in rows:
        d = str(getdate(r.stay_date))
        grid.setdefault(r.room_type, {})[d] = {
            "total": cint(r.total_rooms),
            "sold": cint(r.sold),
            "blocked": cint(r.blocked),
            "ooo": cint(r.out_of_order),
            "available": cint(r.available),
            "stop_sell": cint(r.stop_sell),
            "cta": cint(r.closed_to_arrival),
            "ctd": cint(r.closed_to_departure),
            "min_stay": cint(r.min_stay_override),
            "occupancy": round(
                (cint(r.sold) / cint(r.total_rooms) * 100) if cint(r.total_rooms) else 0, 1),
        }

    payload = {
        "property": property_name,
        "from_date": str(from_date),
        "to_date": str(to_date),
        "nights": date_diff(to_date, from_date) or 1,
        "room_types": [
            frappe.db.get_value("Room Type", rt,
                                ["name", "room_type_name", "short_code",
                                 "base_occupancy", "max_occupancy"], as_dict=True)
            for rt in types
        ],
        "grid": grid,
    }

    if cint(include_rates):
        from .rates import rate_grid
        payload["rates"] = rate_grid(property_name, types, from_date, last_night)

    return payload


@frappe.whitelist()
def check_availability(property_name: str, room_type: str,
                       arrival, departure, rooms: int = 1) -> dict:
    """Yes/no for a specific request, with the reason when it is no."""
    arrival, departure = getdate(arrival), getdate(departure)
    if departure <= arrival:
        return {"available": False, "reason": _("Departure must be after arrival")}

    last_night = add_days(departure, -1)
    ensure_buckets(property_name, room_type, arrival, last_night)
    rooms = cint(rooms) or 1

    rows = frappe.get_all(
        "Room Inventory",
        filters={"property": property_name, "room_type": room_type,
                 "stay_date": ["between", [arrival, last_night]]},
        fields=["stay_date", "available", "stop_sell", "closed_to_arrival",
                "closed_to_departure", "min_stay_override"],
        order_by="stay_date asc")

    nights = date_diff(departure, arrival)
    if len(rows) < nights:
        return {"available": False, "reason": _("Inventory not open for the full range")}

    blockers = []
    min_free = None
    for i, r in enumerate(rows):
        if cint(r.stop_sell):
            blockers.append({"date": str(r.stay_date), "reason": "stop_sell"})
        if i == 0 and cint(r.closed_to_arrival):
            blockers.append({"date": str(r.stay_date), "reason": "closed_to_arrival"})
        if cint(r.available) < rooms:
            blockers.append({"date": str(r.stay_date), "reason": "sold_out",
                             "available": cint(r.available)})
        if cint(r.min_stay_override) and nights < cint(r.min_stay_override):
            blockers.append({"date": str(r.stay_date), "reason": "min_stay",
                             "required": cint(r.min_stay_override)})
        min_free = cint(r.available) if min_free is None else min(min_free, cint(r.available))

    # closed-to-departure applies to the night BEFORE departure
    ctd = frappe.db.get_value(
        "Room Inventory",
        {"property": property_name, "room_type": room_type, "stay_date": last_night},
        "closed_to_departure")
    if cint(ctd):
        blockers.append({"date": str(departure), "reason": "closed_to_departure"})

    return {
        "available": not blockers,
        "max_rooms": min_free or 0,
        "nights": nights,
        "blockers": blockers,
    }


# ---------------------------------------------------------------------------
# writes — the only functions permitted to move `sold`
# ---------------------------------------------------------------------------

def _lock_range(property_name, room_type, first_night, last_night):
    """SELECT ... FOR UPDATE across the range. Serialises concurrent
    bookings for the same room type without locking the whole table."""
    return frappe.db.sql("""
        SELECT name, stay_date, total_rooms, sold, blocked,
               out_of_order, overbook_limit, stop_sell
          FROM `tabRoom Inventory`
         WHERE property = %s AND room_type = %s
           AND stay_date BETWEEN %s AND %s
         ORDER BY stay_date
           FOR UPDATE
    """, (property_name, room_type, first_night, last_night), as_dict=True)


def consume(property_name: str, room_type: str, arrival, departure,
            rooms: int = 1, allow_overbook: bool = False) -> None:
    """Decrement availability for a confirmed booking. Raises rather than
    overselling."""
    arrival, departure = getdate(arrival), getdate(departure)
    last_night = add_days(departure, -1)
    rooms = cint(rooms) or 1

    ensure_buckets(property_name, room_type, arrival, last_night)
    rows = _lock_range(property_name, room_type, arrival, last_night)

    nights = date_diff(departure, arrival)
    if len(rows) < nights:
        frappe.throw(_("Inventory rows missing for {0} between {1} and {2}")
                     .format(room_type, arrival, last_night))

    for r in rows:
        capacity = (cint(r.total_rooms) + cint(r.overbook_limit)
                    - cint(r.blocked) - cint(r.out_of_order))
        if cint(r.stop_sell) and not allow_overbook:
            frappe.throw(_("{0} is on stop-sell for {1}").format(room_type, r.stay_date))
        if cint(r.sold) + rooms > capacity and not allow_overbook:
            frappe.throw(
                _("Only {0} {1} room(s) left on {2} — cannot sell {3}")
                .format(max(0, capacity - cint(r.sold)), room_type, r.stay_date, rooms))

    for r in rows:
        frappe.db.set_value("Room Inventory", r.name, "sold",
                            cint(r.sold) + rooms, update_modified=False)
        _recompute_available(r.name)


def release(property_name: str, room_type: str, arrival, departure,
            rooms: int = 1) -> None:
    """Give inventory back on cancel, no-show release, or amendment."""
    arrival, departure = getdate(arrival), getdate(departure)
    last_night = add_days(departure, -1)
    rooms = cint(rooms) or 1

    rows = _lock_range(property_name, room_type, arrival, last_night)
    for r in rows:
        frappe.db.set_value("Room Inventory", r.name, "sold",
                            max(0, cint(r.sold) - rooms), update_modified=False)
        _recompute_available(r.name)


def block(property_name: str, room_type: str, from_date, to_date,
          rooms: int, release_existing: bool = False) -> None:
    """Hold rooms for a corporate allotment or an OTA pool."""
    from_date, to_date = getdate(from_date), getdate(to_date)
    ensure_buckets(property_name, room_type, from_date, to_date)
    rows = _lock_range(property_name, room_type, from_date, to_date)
    for r in rows:
        new = cint(rooms) if release_existing else cint(r.blocked) + cint(rooms)
        frappe.db.set_value("Room Inventory", r.name, "blocked",
                            max(0, new), update_modified=False)
        _recompute_available(r.name)


# ---------------------------------------------------------------------------
# reconciliation — run by the night audit
# ---------------------------------------------------------------------------

def reconcile(property_name: str, from_date=None, to_date=None) -> list[dict]:
    """Recompute `sold` from live reservations and report every square
    that disagreed with the ledger.

    A PMS that never checks this drifts silently and someone eventually
    walks a guest. Running it nightly turns a silent failure into a line
    in the audit log.
    """
    from_date = getdate(from_date or nowdate())
    to_date = getdate(to_date or add_days(from_date, 365))

    actual = frappe.db.sql("""
        SELECT rr.room_type, d.stay_date, COUNT(*) AS sold
          FROM `tabReservation Room` rr
          JOIN `tabReservation` r ON r.name = rr.parent
          JOIN (
              SELECT stay_date FROM `tabRoom Inventory`
               WHERE property = %(prop)s
                 AND stay_date BETWEEN %(from)s AND %(to)s
               GROUP BY stay_date
          ) d ON d.stay_date >= rr.arrival_date
             AND d.stay_date <  rr.departure_date
         WHERE r.property = %(prop)s
           AND r.docstatus = 1
           AND r.status IN ('Confirmed', 'Checked In', 'Tentative')
      GROUP BY rr.room_type, d.stay_date
    """, {"prop": property_name, "from": from_date, "to": to_date}, as_dict=True)

    actual_map = {(a.room_type, getdate(a.stay_date)): cint(a.sold) for a in actual}

    ledger = frappe.get_all(
        "Room Inventory",
        filters={"property": property_name,
                 "stay_date": ["between", [from_date, to_date]]},
        fields=["name", "room_type", "stay_date", "sold"])

    drift = []
    for row in ledger:
        expected = actual_map.get((row.room_type, getdate(row.stay_date)), 0)
        if cint(row.sold) != expected:
            drift.append({
                "room_type": row.room_type,
                "date": str(getdate(row.stay_date)),
                "ledger_sold": cint(row.sold),
                "actual_sold": expected,
                "delta": expected - cint(row.sold),
            })
            frappe.db.set_value("Room Inventory", row.name, "sold", expected,
                                update_modified=False)
            _recompute_available(row.name)

    if drift:
        frappe.db.commit()
    return drift
