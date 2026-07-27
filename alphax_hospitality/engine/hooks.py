"""Document hooks."""

import frappe
from frappe.utils import nowdate

from . import availability


def room_changed(doc, method=None):
    """Adding, deactivating or OOO-ing a room changes the physical
    count. Re-seed forward inventory so availability stays honest
    without anyone remembering to rebuild it."""
    frappe.enqueue(
        "alphax_hospitality.alphax_hospitality.engine.availability.rebuild_totals",
        queue="short", enqueue_after_commit=True,
        property_name=doc.property, room_type=doc.room_type, from_date=nowdate())


def reservation_cancelled(doc, method=None):
    """Cancelling the DOCUMENT (as opposed to the business action)
    must still return inventory."""
    for rr in doc.rooms:
        try:
            availability.release(doc.property, rr.room_type,
                                 rr.arrival_date, rr.departure_date, 1)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "release on cancel")
