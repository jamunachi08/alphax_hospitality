"""Code-driven setup. Every function is idempotent - mandatory on
Frappe Cloud where the only lever is a migrate."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ROLES = [
    "Hotel Manager", "Front Desk Agent", "Housekeeping Supervisor",
    "Housekeeping Attendant", "Night Auditor", "Reservations Agent",
]

# Every folio line resolves to an Item so revenue lands in the right
# income account with the right tax treatment.
SERVICE_ITEMS = [
    ("HOTEL-ROOM-REVENUE", "Room Revenue"),
    ("HOTEL-FB", "Food & Beverage"),
    ("HOTEL-MINIBAR", "Minibar"),
    ("HOTEL-LAUNDRY", "Laundry"),
    ("HOTEL-SPA", "Spa & Wellness"),
    ("HOTEL-TELEPHONE", "Telephone"),
    ("HOTEL-MISC", "Miscellaneous"),
    ("HOTEL-LEVY", "Municipality Levy"),
]

CUSTOM_FIELDS = {
    "Sales Invoice": [
        dict(fieldname="alphax_property", label="Hotel Property",
             fieldtype="Link", options="Hotel Property",
             insert_after="cost_center", read_only=1),
        dict(fieldname="alphax_folio", label="Guest Folio",
             fieldtype="Link", options="Guest Folio",
             insert_after="alphax_property", read_only=1),
    ],
    "Customer": [
        dict(fieldname="alphax_guest_profile", label="Guest Profile",
             fieldtype="Link", options="Guest Profile",
             insert_after="customer_group", read_only=1),
    ],
}


def after_install():
    create_roles()
    create_service_items()
    ensure_custom_fields()
    create_workspace()
    frappe.db.commit()


def after_migrate():
    ensure_custom_fields()
    create_service_items()
    frappe.db.commit()


def create_roles():
    for name in ROLES:
        if frappe.db.exists("Role", name):
            continue
        frappe.get_doc({"doctype": "Role", "role_name": name,
                        "desk_access": 1}).insert(ignore_permissions=True)


def _ensure_item_group():
    name = "Hotel Services"
    if not frappe.db.exists("Item Group", name):
        parent = frappe.db.get_value("Item Group", {"is_group": 1}, "name") \
            or "All Item Groups"
        frappe.get_doc({"doctype": "Item Group", "item_group_name": name,
                        "parent_item_group": parent,
                        "is_group": 0}).insert(ignore_permissions=True)
    return name


def create_service_items():
    group = _ensure_item_group()
    for code, name in SERVICE_ITEMS:
        if frappe.db.exists("Item", code):
            continue
        try:
            frappe.get_doc({
                "doctype": "Item", "item_code": code, "item_name": name,
                "item_group": group, "stock_uom": "Nos",
                "is_stock_item": 0, "is_sales_item": 1, "is_purchase_item": 0,
            }).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"hospitality item {code}")


def ensure_custom_fields():
    try:
        create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "hospitality custom fields")


def create_workspace():
    if frappe.db.exists("Workspace", "Hospitality"):
        return
    ws = frappe.get_doc({
        "doctype": "Workspace", "name": "Hospitality", "label": "Hospitality",
        "module": "AlphaX Hospitality", "icon": "hotel",
        "public": 1, "is_hidden": 0, "content": "[]",
    })
    for label, dt in [
        ("Front Desk", "Reservation"), ("Guest Folio", "Guest Folio"),
        ("Rooms", "Room"), ("Housekeeping", "Housekeeping Task"),
        ("Room Inventory", "Room Inventory"), ("Rate Plans", "Rate Plan"),
        ("Guests", "Guest Profile"), ("Night Audit", "Night Audit Log"),
    ]:
        ws.append("shortcuts", {"type": "DocType", "link_to": dt, "label": label})
    ws.insert(ignore_permissions=True)
