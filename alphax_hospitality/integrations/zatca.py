"""
ZATCA e-invoicing for folio invoices.

Two shapes matter at a front desk:

  Simplified (B2C) - walk-in guest, no VAT number. QR on the printed
                     copy, reported within 24h. Issues instantly, so
                     checkout never blocks.

  Standard (B2B)   - corporate guest with a VAT registration. Must be
                     CLEARED by ZATCA BEFORE issuance. That is a
                     synchronous government API call in the middle of
                     a checkout.

The second is the operational trap: if the gateway is slow, a guest
with a taxi waiting stands at the desk. So `zatca_async` on the
property queues clearance, prints a proforma noting the tax invoice
will follow by email, and retries hourly.

Signing and clearance are delegated to whichever ZATCA app the site
already runs rather than reimplementing CSR generation and XML
signing. This module only decides WHEN, and owns the queue and retry.
"""

import frappe
from frappe.utils import cint

ADAPTERS = [
    "alphax_pos_suite.alphax_pos_suite.integrations.zatca_adapter.clear_invoice",
    "zatca_erpgulf.zatca_erpgulf.sign_invoice.zatca_call",
]


def on_invoice_submit(doc, method=None):
    if not doc.get("alphax_property"):
        return
    prop = frappe.get_cached_doc("Hotel Property", doc.alphax_property)
    if not cint(prop.get("zatca_enabled")):
        return
    if cint(prop.get("zatca_async")):
        frappe.enqueue(
            "alphax_hospitality.alphax_hospitality.integrations.zatca.clear_invoice",
            queue="short", enqueue_after_commit=True, invoice=doc.name)
    else:
        clear_invoice(doc.name)


def clear_invoice(invoice: str):
    handler = _resolve_adapter()
    if not handler:
        frappe.log_error(f"No ZATCA adapter installed; {invoice} not cleared",
                         "hospitality zatca")
        return {"ok": False, "reason": "no_adapter"}
    try:
        return handler(invoice)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"zatca clearance {invoice}")
        return {"ok": False}


def _resolve_adapter():
    for path in ADAPTERS:
        try:
            return frappe.get_attr(path)
        except Exception:
            continue
    return None


def retry_failed_clearances():
    try:
        rows = frappe.get_all("Sales Invoice", filters={
            "docstatus": 1,
            "custom_zatca_status": ["in", ["Failed", "Pending"]],
        }, pluck="name", limit=200)
    except Exception:
        return 0
    for name in rows:
        clear_invoice(name)
    return len(rows)
