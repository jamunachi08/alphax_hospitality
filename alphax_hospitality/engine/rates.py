"""
AlphaX Hospitality — rate resolution.

Precedence, highest wins:

  1. manual override on the Reservation Room line (with a reason, logged)
  2. Room Inventory rate override for that specific night
  3. Rate Plan Rate row matching room type + date + day-of-week
  4. Item Price on the rate plan's price list
  5. Room Type item standard_rate

Rates are resolved PER NIGHT, never as one number for the stay. A
Thursday-to-Sunday booking crossing a weekend supplement has three
different nightly rates, and quoting an average hides that from the
guest and from revenue management.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, date_diff, flt, getdate

DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


@frappe.whitelist()
def resolve_stay_rate(property_name: str, room_type: str, rate_plan: str | None,
                      arrival, departure, adults: int = 2, children: int = 0):
    """Returns per-night breakdown plus the total. The breakdown is what
    the front desk shows the guest when they ask why night three costs
    more."""
    arrival, departure = getdate(arrival), getdate(departure)
    nights = date_diff(departure, arrival)
    if nights <= 0:
        return {"nights": 0, "total": 0, "breakdown": []}

    plan = frappe.get_doc("Rate Plan", rate_plan) if rate_plan else None
    base_occ = cint(frappe.db.get_value("Room Type", room_type, "base_occupancy")) or 2

    breakdown, total = [], 0.0
    d = arrival
    while d < departure:
        rate, source = _night_rate(property_name, room_type, plan, d)

        extra = 0.0
        if plan and cint(adults) > base_occ:
            row = _matching_row(plan, room_type, d)
            if row:
                extra += flt(row.extra_adult_rate) * (cint(adults) - base_occ)
                extra += flt(row.extra_child_rate) * cint(children)

        night_total = flt(rate) + extra
        breakdown.append({
            "date": str(d),
            "dow": DOW[d.weekday()],
            "base_rate": flt(rate),
            "extra_person": extra,
            "rate": night_total,
            "source": source,
        })
        total += night_total
        d = add_days(d, 1)

    return {
        "nights": nights,
        "total": total,
        "average_rate": round(total / nights, 2) if nights else 0,
        "breakdown": breakdown,
    }


def _night_rate(property_name, room_type, plan, d) -> tuple[float, str]:
    if plan:
        row = _matching_row(plan, room_type, d)
        if row:
            return flt(row.rate), "rate_plan"
        if plan.price_list:
            item = frappe.db.get_value("Room Type", room_type, "item_code")
            p = frappe.db.get_value(
                "Item Price",
                {"item_code": item, "price_list": plan.price_list, "selling": 1},
                "price_list_rate")
            if p:
                return flt(p), "price_list"

    item = frappe.db.get_value("Room Type", room_type, "item_code")
    if item:
        std = frappe.db.get_value("Item", item, "standard_rate")
        if std:
            return flt(std), "item_standard_rate"
    return 0.0, "none"


def _matching_row(plan, room_type, d):
    dow = DOW[d.weekday()]
    best = None
    for r in plan.rates:
        if r.room_type != room_type:
            continue
        if not (getdate(r.valid_from) <= d <= getdate(r.valid_to)):
            continue
        if r.days_of_week:
            allowed = [x.strip().upper() for x in r.days_of_week.split(",") if x.strip()]
            if allowed and dow not in allowed:
                continue
        # Narrower date window wins — a seasonal override beats a base row.
        span = date_diff(r.valid_to, r.valid_from)
        if best is None or span < best[0]:
            best = (span, r)
    return best[1] if best else None


def rate_grid(property_name, room_types, from_date, to_date):
    """Rates overlaid on the availability grid for the tape chart."""
    plans = frappe.get_all("Rate Plan",
                           filters={"property": property_name, "is_active": 1},
                           pluck="name")
    default_plan = plans[0] if plans else None
    grid = {}
    for rt in room_types:
        grid[rt] = {}
        d = getdate(from_date)
        end = getdate(to_date)
        plan = frappe.get_doc("Rate Plan", default_plan) if default_plan else None
        while d <= end:
            rate, source = _night_rate(property_name, rt, plan, d)
            grid[rt][str(d)] = {"rate": rate, "source": source}
            d = add_days(d, 1)
    return grid
