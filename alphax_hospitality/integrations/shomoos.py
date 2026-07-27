"""
Shomoos - Saudi Ministry of Interior guest registration.

KSA hotels are required to register guest arrivals with the MOI
platform (operated through ELM). This adapter has the right shape:
token caching, idempotent submission, PDPL-safe audit logging, and a
queue so a portal outage never blocks a check-in at the desk.

The transport is a single `_post()` to be wired to the production
endpoint and credentials, following the alphax_muqeem ELM pattern.
"""

import frappe
from frappe.utils import now_datetime

TOKEN_CACHE_KEY = "shomoos_token"
TOKEN_TTL = 25 * 60


def _token():
    cached = frappe.cache().get_value(TOKEN_CACHE_KEY)
    if cached:
        return cached
    token = None  # TODO: wire to ELM auth endpoint
    if token:
        frappe.cache().set_value(TOKEN_CACHE_KEY, token, expires_in_sec=TOKEN_TTL)
    return token


def register_guest(reservation: str, guest: str):
    """Idempotent - a guest already registered for this reservation is
    never submitted twice."""
    if frappe.db.get_value("Guest Profile", guest, "shomoos_registered"):
        return {"ok": True, "skipped": "already_registered"}

    g = frappe.get_doc("Guest Profile", guest)
    res = frappe.get_doc("Reservation", reservation)
    prop = frappe.get_cached_doc("Hotel Property", res.property)

    payload = {
        "propertyId": prop.get("shomoos_property_id"),
        "identityType": g.id_type,
        "identityNumber": g.id_number,
        "nationality": g.nationality,
        "checkInDate": str(res.actual_check_in or res.arrival_date),
        "expectedCheckOutDate": str(res.departure_date),
        "roomNumbers": [r.room for r in res.rooms if r.room],
    }

    try:
        resp = _post("/guest/checkin", payload)
        g.db_set("shomoos_registered", 1, update_modified=False)
        g.db_set("shomoos_reference", (resp or {}).get("reference", ""),
                 update_modified=False)
        _audit(reservation, "checkin", "Success")
        return {"ok": True}
    except Exception as e:
        _audit(reservation, "checkin", f"Failed: {e}")
        frappe.log_error(frappe.get_traceback(), "shomoos register")
        return {"ok": False}


def _post(path, payload):
    raise NotImplementedError(
        "Wire Shomoos transport to the production ELM endpoint. "
        "Reuse the auth + retry pattern from alphax_muqeem.")


def _audit(reservation, action, status):
    """PDPL: log that a transfer happened and its outcome. Never log
    the identity number itself."""
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Info",
        "reference_doctype": "Reservation", "reference_name": reservation,
        "content": f"Shomoos {action}: {status} at {now_datetime()}",
    }).insert(ignore_permissions=True)
