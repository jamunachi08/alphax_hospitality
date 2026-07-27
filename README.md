# AlphaX Hospitality

Hotel Property Management System for **ERPNext v15**. Built as a domain app
in the shape of `hrms` / `healthcare` — it sits on top of ERPNext rather
than replacing any part of it.

`frappe/hospitality` was archived on 4 Oct 2023 (36 commits, docs only ever
reached v13). This is a clean-sheet v15 app, not a fork.

## Scope

| Area | Covered |
|---|---|
| Reservations | quote, book, amend, cancel, no-show |
| Availability | daily inventory buckets, overbooking policy, allotments, ARI-shaped |
| Rates | rate plans, seasonal + day-of-week rows, extra person, per-night resolution |
| Front desk | arrivals/departures console, room assignment, check-in/out |
| Folios | staging ledger, routing, split billing, transfers, city ledger |
| Night audit | idempotent, business-date driven, reconciliation, rollback |
| Housekeeping | departure/stayover tasks, room status board |
| Compliance | ZATCA (sync + async clearance), Shomoos hook, PDPL retention |
| POS bridge | charge restaurant/bar checks straight to the guest folio |

## Four architecture decisions

**Inventory buckets, not reservation scans.** `Room Inventory` holds one row
per property + room type + night. Booking takes a `FOR UPDATE` lock across
the date range and decrements `sold`. Availability becomes an indexed range
read instead of an O(reservations) scan, and the bucket is already the exact
ARI shape a channel manager wants. The night audit reconciles buckets against
reservations and logs any drift rather than letting it accumulate silently.

**Folio as staging ledger.** Charges accrue on the folio with zero GL impact.
Checkout collapses them into one Sales Invoice, grouped by item and rate, so
a 14-night stay produces one room line at qty 14 — not 14 lines, and
certainly not 14 invoices for ZATCA to clear. Month-end cutoff is handled by
an interim invoice run for in-house guests.

**Business date, never `nowdate()`.** Every charge, rate lookup and
availability decision keys off `Hotel Property.business_date`, advanced only
by the night audit. A charge posted at 01:30 belongs to yesterday. Systems
that use the server clock here produce a P&L that never ties to the occupancy
report.

**Departure day is not a night.** A stay from the 14th to the 17th consumes
nights 14, 15 and 16. Enforced in one place (`engine/availability.py`); every
caller inherits it.

## Install

```bash
bench get-app https://github.com/jamunachi08/alphax_hospitality
bench --site <site> install-app alphax_hospitality
```

Frappe Cloud: add the repo to the bench, then Migrate. All setup is
code-driven (`install.py`) — no fixtures, so nothing drifts.

## Post-install

1. Create a **Hotel Property**: company, cost center, tax template,
   check-in/out times, night audit time.
2. Set `business_date` to today. The night audit takes over from there.
3. Create **Room Types**, each linked to a non-stock Item.
4. Create **Rooms**.
5. Create at least one **Rate Plan** with rate rows.

Availability buckets are created lazily on first read — no seeding step.

## API surface

```
engine.availability.get_availability(property, from, to)     # tape chart grid
engine.availability.check_availability(...)                  # yes/no + blockers
engine.rates.resolve_stay_rate(...)                          # per-night breakdown
api.frontdesk.console(property)                              # front desk home
api.frontdesk.quote(...)                                     # availability + price
api.frontdesk.create_reservation(payload)
api.frontdesk.check_in(reservation, room_assignments)
api.frontdesk.check_out(reservation)
api.frontdesk.find_in_house(property, query)                 # POS: room lookup
api.frontdesk.charge_to_room(folio, amount, description)     # POS: post to folio
engine.folio.post_charge / post_payment / checkout
engine.night_audit.run(property) / rollback_last(property, reason)
```

## Not yet built

- Front-desk SPA (tape chart, drag-and-drop assignment). API is complete;
  the Vue app is phase 2.
- Channel manager connector. `Room Inventory` is already the right shape.
- Revenue management / dynamic pricing.
