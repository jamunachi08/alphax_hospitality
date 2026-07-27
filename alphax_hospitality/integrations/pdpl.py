"""PDPL retention. Guest personal data is purged after the retention
window, measured from last departure."""

import frappe
from frappe.utils import add_days, cint, getdate, nowdate

DEFAULT_RETENTION_DAYS = 365 * 3


def purge_expired_guest_data():
    rows = frappe.get_all("Guest Profile",
                          filters={"retention_until": ["<", getdate(nowdate())]},
                          pluck="name")
    for name in rows:
        try:
            frappe.db.set_value("Guest Profile", name, {
                "id_number": "[purged]", "id_document": None, "mobile": None,
                "email": None, "address": None, "date_of_birth": None,
                "visa_number": None,
            }, update_modified=False)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"pdpl purge {name}")
    if rows:
        frappe.db.commit()
    return len(rows)


def stamp_retention(guest: str, departure_date, days=DEFAULT_RETENTION_DAYS):
    frappe.db.set_value("Guest Profile", guest, "retention_until",
                        add_days(getdate(departure_date), cint(days)),
                        update_modified=False)
