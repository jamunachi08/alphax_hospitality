"""Scheduler glue. Separate module so hooks.py points at stable paths."""

import frappe
from frappe.utils import get_datetime, getdate, now_datetime, nowdate

from . import night_audit


def maybe_run_night_audit():
    """Every 15 min; each property fires once, when its local clock
    passes its configured audit time and today's run has not completed."""
    now = now_datetime()
    for name in frappe.get_all("Hotel Property", filters={"is_active": 1}, pluck="name"):
        try:
            prop = frappe.get_cached_doc("Hotel Property", name)
            if not prop.night_audit_time:
                continue
            if now < get_datetime(f"{nowdate()} {prop.night_audit_time}"):
                continue
            if prop.last_night_audit and getdate(prop.last_night_audit) == getdate(now):
                continue
            night_audit.run(name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"night audit scheduler: {name}")


def raise_stayover_housekeeping():
    for prop in frappe.get_all("Hotel Property", filters={"is_active": 1}, pluck="name"):
        rooms = frappe.get_all("Room", filters={
            "property": prop, "is_active": 1,
            "status": ["in", ["Occupied Clean", "Occupied Dirty"]],
        }, pluck="name")
        for room in rooms:
            if frappe.db.exists("Housekeeping Task", {
                "room": room, "task_date": nowdate(), "task_type": "Stayover Clean",
            }):
                continue
            frappe.get_doc({
                "doctype": "Housekeeping Task", "property": prop, "room": room,
                "task_date": nowdate(), "task_type": "Stayover Clean",
                "priority": "Normal", "status": "Pending",
            }).insert(ignore_permissions=True)
    frappe.db.commit()
