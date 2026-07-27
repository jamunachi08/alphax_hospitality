app_name = "alphax_hospitality"
app_title = "AlphaX Hospitality"
app_publisher = "Neotec Integrated Solution"
app_description = "Hotel Property Management System for ERPNext v15"
app_email = "support@neotec.ai"
app_license = "MIT"

required_apps = ["frappe/erpnext"]

# Setup is code-driven. No fixture JSON - Frappe Cloud gives us no
# bench shell and fixtures drift between environments.
after_install = "alphax_hospitality.alphax_hospitality.install.after_install"
after_migrate = "alphax_hospitality.alphax_hospitality.install.after_migrate"

scheduler_events = {
    "cron": {
        # Checked every 15 min; each property fires only when its own
        # configured audit time passes. One entry, N local schedules.
        "*/15 * * * *": [
            "alphax_hospitality.alphax_hospitality.engine.scheduler.maybe_run_night_audit",
        ],
    },
    "daily": [
        "alphax_hospitality.alphax_hospitality.engine.scheduler.raise_stayover_housekeeping",
        "alphax_hospitality.alphax_hospitality.integrations.pdpl.purge_expired_guest_data",
    ],
    "hourly": [
        "alphax_hospitality.alphax_hospitality.integrations.zatca.retry_failed_clearances",
    ],
}

doc_events = {
    "Room": {
        "after_insert": "alphax_hospitality.alphax_hospitality.engine.hooks.room_changed",
        "on_update": "alphax_hospitality.alphax_hospitality.engine.hooks.room_changed",
    },
    "Reservation": {
        "on_cancel": "alphax_hospitality.alphax_hospitality.engine.hooks.reservation_cancelled",
    },
    "Sales Invoice": {
        "on_submit": "alphax_hospitality.alphax_hospitality.integrations.zatca.on_invoice_submit",
    },
}

website_route_rules = [
    {"from_route": "/booking/<property_slug>", "to_route": "booking"},
]
