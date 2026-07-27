"""
AlphaX Hospitality — guest folio.

The folio is a STAGING LEDGER, not a GL document. Charges accumulate on
it all stay long with no accounting impact. At checkout it collapses into
one (or a few) Sales Invoices.

Why not post every charge to the GL as it happens?

  - A 14-night stay with breakfast, minibar and laundry generates 60+
    postings. As Journal Entries that is unreadable and unreconcilable.
  - Charges get moved, split, routed to a company, disputed and reversed
    while the guest is in house. Reversing GL entries for a minibar
    charge the guest denies is absurd; deleting a folio line is not.
  - ZATCA needs ONE invoice per commercial transaction. Sixty invoices
    for one stay would be both wrong and expensive to clear.

So: folio lines carry the Item, qty, rate and tax. On checkout we build a
Sales Invoice from them. Revenue recognition timing is preserved because
the night audit posts each night's room charge on that night's business
date, and month-end cutoff is handled by an interim invoice run for
in-house guests.

Routing is the other reason the folio exists. A corporate guest whose
company pays room-and-tax but who pays their own bar tab needs two
invoices from one stay. That is a folio operation, not an invoice one.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

# Which txn types are "room and tax" for routing purposes.
ROOM_TYPES = {"Room", "Tax", "Levy"}
CREDIT_TYPES = {"Payment", "Deposit", "Refund"}


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def open_folio(reservation: str, folio_type: str = "Guest") -> str:
    """One folio per reservation, created at check-in."""
    res = frappe.get_doc("Reservation", reservation)
    if res.folio and frappe.db.exists("Guest Folio", res.folio):
        return res.folio

    doc = frappe.get_doc({
        "doctype": "Guest Folio",
        "property": res.property,
        "reservation": res.name,
        "guest": res.guest,
        "customer": res.customer,
        "folio_type": folio_type,
        "status": "Open",
        "room": ", ".join([r.room for r in res.rooms if r.room]),
    })
    doc.insert(ignore_permissions=True)
    res.db_set("folio", doc.name, update_modified=False)
    return doc.name


def post(folio: str, *, txn_type: str, description: str,
         charge: float = 0, credit: float = 0, item_code: str | None = None,
         qty: float = 1, rate: float = 0, tax_amount: float = 0,
         business_date=None, source: str = "Front Desk",
         reference_doctype: str | None = None,
         reference_name: str | None = None) -> str:
    """Append one line. The only way anything lands on a folio."""
    doc = frappe.get_doc("Guest Folio", folio)
    if doc.status != "Open":
        frappe.throw(_("Folio {0} is {1} — cannot post to it").format(folio, doc.status))

    business_date = business_date or _business_date(doc.property)

    row = doc.append("transactions", {
        "posting_date": nowdate(),
        "business_date": business_date,
        "txn_type": txn_type,
        "description": description,
        "item_code": item_code,
        "qty": flt(qty),
        "rate": flt(rate),
        "charge": flt(charge),
        "credit": flt(credit),
        "tax_amount": flt(tax_amount),
        "source": source,
        "posted_by": frappe.session.user,
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
    })
    _recompute(doc)
    doc.save(ignore_permissions=True)

    # Routing runs after the line exists so the routed copy references it.
    if doc.route_to_folio and doc.routing_rule and txn_type not in CREDIT_TYPES:
        _apply_routing(doc, row)

    return row.name


def _apply_routing(doc, row):
    """Move a charge to the master folio per the routing rule."""
    rule = doc.routing_rule
    t = row.txn_type
    should_route = (
        (rule == "All to Master") or
        (rule == "Room & Tax to Master" and t in ROOM_TYPES) or
        (rule == "Room Only to Master" and t == "Room")
    )
    if not should_route or cint(row.is_routed):
        return

    post(doc.route_to_folio,
         txn_type=t,
         description=f"{row.description} (routed from {doc.name})",
         charge=flt(row.charge), item_code=row.item_code,
         qty=flt(row.qty), rate=flt(row.rate), tax_amount=flt(row.tax_amount),
         business_date=row.business_date, source="Interface",
         reference_doctype="Guest Folio", reference_name=doc.name)

    # Zero it here and mark it routed rather than deleting — the guest's
    # folio must still SHOW the charge, with zero balance impact.
    frappe.db.set_value("Folio Transaction", row.name,
                        {"charge": 0, "tax_amount": 0, "is_routed": 1},
                        update_modified=False)
    _recompute(frappe.get_doc("Guest Folio", doc.name), save=True)


def _recompute(doc, save=False):
    charges = taxes = credits = 0.0
    for t in doc.transactions:
        if cint(t.voided):
            continue
        charges += flt(t.charge)
        taxes += flt(t.tax_amount)
        credits += flt(t.credit)
    doc.total_charges = charges
    doc.total_taxes = taxes
    doc.total_credits = credits
    doc.balance = charges + taxes - credits
    if save:
        doc.save(ignore_permissions=True)
    return doc


def _business_date(property_name):
    return frappe.db.get_value("Hotel Property", property_name, "business_date") or nowdate()


# ---------------------------------------------------------------------------
# transfers and adjustments
# ---------------------------------------------------------------------------

def transfer_line(folio: str, row_name: str, to_folio: str, reason: str = "") -> None:
    """Move a single charge between folios — the split-bill primitive the
    front desk actually uses ('the bar tab goes on his card, not the
    company's')."""
    row = frappe.get_doc("Folio Transaction", row_name)
    if row.parent != folio:
        frappe.throw(_("Line does not belong to folio {0}").format(folio))

    post(to_folio, txn_type=row.txn_type,
         description=f"{row.description} (transferred){' — ' + reason if reason else ''}",
         charge=flt(row.charge), item_code=row.item_code, qty=flt(row.qty),
         rate=flt(row.rate), tax_amount=flt(row.tax_amount),
         business_date=row.business_date, source="Interface",
         reference_doctype="Guest Folio", reference_name=folio)

    frappe.db.set_value("Folio Transaction", row_name,
                        {"charge": 0, "tax_amount": 0, "voided": 1,
                         "description": f"{row.description} → {to_folio}"},
                        update_modified=False)
    _recompute(frappe.get_doc("Guest Folio", folio), save=True)


def void_line(folio: str, row_name: str, reason: str) -> None:
    """Voids never delete. An auditor must be able to see that a charge
    was raised and withdrawn, and by whom."""
    frappe.db.set_value("Folio Transaction", row_name, {
        "voided": 1,
        "description": f"{frappe.db.get_value('Folio Transaction', row_name, 'description')} "
                       f"[VOID: {reason} — {frappe.session.user}]",
    }, update_modified=False)
    _recompute(frappe.get_doc("Guest Folio", folio), save=True)


# ---------------------------------------------------------------------------
# checkout -> Sales Invoice
# ---------------------------------------------------------------------------

def build_invoice(folio: str, *, submit: bool = True,
                  is_interim: bool = False) -> str:
    """Collapse the folio into one Sales Invoice.

    Groups identical (item, rate) lines so a 14-night stay produces one
    room line at qty 14 rather than 14 lines — which is what a guest
    expects to see and what ZATCA clears cleanly.
    """
    doc = frappe.get_doc("Guest Folio", folio)
    prop = frappe.get_doc("Hotel Property", doc.property)

    if not doc.customer:
        frappe.throw(_("Folio {0} has no Bill To customer").format(folio))

    charges = [t for t in doc.transactions
               if not cint(t.voided) and flt(t.charge) > 0 and t.txn_type not in CREDIT_TYPES]
    if not charges:
        frappe.throw(_("Nothing to invoice on folio {0}").format(folio))

    grouped: dict[tuple, dict] = {}
    for t in charges:
        item = t.item_code or _fallback_item(prop, t.txn_type)
        key = (item, flt(t.rate), t.description if flt(t.qty) == 1 else None)
        g = grouped.setdefault(key, {
            "item_code": item, "rate": flt(t.rate), "qty": 0,
            "description": t.description,
        })
        g["qty"] += flt(t.qty) or 1

    si = frappe.new_doc("Sales Invoice")
    si.customer = doc.customer
    si.company = prop.company
    si.posting_date = _business_date(doc.property)
    si.due_date = si.posting_date
    si.cost_center = prop.cost_center
    si.alphax_folio = folio          # custom field, see install.py
    si.alphax_property = doc.property
    if prop.tax_template:
        si.taxes_and_charges = prop.tax_template
        si.set("taxes", frappe.get_doc(
            "Sales Taxes and Charges Template", prop.tax_template).taxes)

    for g in grouped.values():
        si.append("items", {
            "item_code": g["item_code"],
            "qty": g["qty"] or 1,
            "rate": g["rate"],
            "description": g["description"],
            "cost_center": prop.cost_center,
        })

    si.flags.ignore_permissions = True
    si.insert()

    if submit:
        si.submit()
        _settle_credits(doc, si)

    if not is_interim:
        doc.db_set("sales_invoice", si.name, update_modified=False)
        doc.db_set("status", "Closed", update_modified=False)
        doc.db_set("closed_on", now_datetime(), update_modified=False)
        doc.db_set("closed_by", frappe.session.user, update_modified=False)

    return si.name


def _fallback_item(prop, txn_type):
    """Every folio line must map to an Item. When one wasn't supplied,
    fall back to a per-type service item created at install."""
    code = {
        "Room": "HOTEL-ROOM-REVENUE",
        "Food & Beverage": "HOTEL-FB",
        "Minibar": "HOTEL-MINIBAR",
        "Laundry": "HOTEL-LAUNDRY",
        "Spa": "HOTEL-SPA",
        "Telephone": "HOTEL-TELEPHONE",
    }.get(txn_type, "HOTEL-MISC")
    if not frappe.db.exists("Item", code):
        frappe.throw(_("Service item {0} is missing. Run the hospitality setup.").format(code))
    return code


def _settle_credits(folio_doc, si):
    """Apply deposits and in-stay payments already sitting on the folio
    against the freshly-submitted invoice."""
    credits = sum(flt(t.credit) for t in folio_doc.transactions if not cint(t.voided))
    if credits <= 0:
        return

    prop = frappe.get_doc("Hotel Property", folio_doc.property)
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = prop.company
    pe.party_type = "Customer"
    pe.party = folio_doc.customer
    pe.paid_amount = pe.received_amount = min(credits, flt(si.grand_total))
    pe.paid_to = prop.deposit_account or _default_cash(prop.company)
    pe.reference_no = folio_doc.name
    pe.reference_date = si.posting_date
    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": si.name,
        "allocated_amount": pe.paid_amount,
    })
    pe.flags.ignore_permissions = True
    pe.insert()
    pe.submit()


def _default_cash(company):
    return frappe.db.get_value("Company", company, "default_cash_account")


# ---------------------------------------------------------------------------
# whitelisted wrappers for the front-desk SPA
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_folio(folio: str):
    doc = frappe.get_doc("Guest Folio", folio)
    doc.check_permission("read")
    return doc.as_dict()


@frappe.whitelist()
def post_charge(folio: str, txn_type: str, description: str, amount: float,
                item_code: str | None = None, qty: float = 1):
    amount = flt(amount)
    return post(folio, txn_type=txn_type, description=description,
                charge=amount, item_code=item_code, qty=flt(qty) or 1,
                rate=amount / (flt(qty) or 1))


@frappe.whitelist()
def post_payment(folio: str, amount: float, mode_of_payment: str,
                 reference: str | None = None):
    return post(folio, txn_type="Payment",
                description=f"{mode_of_payment}{' · ' + reference if reference else ''}",
                credit=flt(amount))


@frappe.whitelist()
def checkout(folio: str):
    """Close the folio, raise the invoice, free the room, queue the ZATCA
    clearance if the property is async."""
    doc = frappe.get_doc("Guest Folio", folio)
    doc.check_permission("write")

    invoice = build_invoice(folio, submit=True)

    if doc.reservation:
        res = frappe.get_doc("Reservation", doc.reservation)
        res.db_set("status", "Checked Out", update_modified=False)
        res.db_set("actual_check_out", now_datetime(), update_modified=False)
        res.db_set("sales_invoice", invoice, update_modified=False)
        for r in res.rooms:
            if r.room:
                frappe.db.set_value("Room", r.room, "status", "Vacant Dirty",
                                    update_modified=False)
                _raise_housekeeping(res.property, r.room)

    frappe.db.commit()
    return {"ok": True, "invoice": invoice, "balance": flt(doc.balance)}


def _raise_housekeeping(property_name, room):
    frappe.get_doc({
        "doctype": "Housekeeping Task",
        "property": property_name,
        "room": room,
        "task_date": nowdate(),
        "task_type": "Departure Clean",
        "priority": "High",
        "status": "Pending",
    }).insert(ignore_permissions=True)
