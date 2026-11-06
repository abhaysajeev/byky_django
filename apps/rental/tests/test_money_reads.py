"""Guard: no new code may read money without deciding about cancelled orders.

Owner, 6 Nov 2026: a cancelled order's money counts nowhere
(apps/rental/counted.py). Every place that reads Payment, Invoice,
InvoiceItem, CreditNote or card-discount claims was reviewed for that rule.
This test lists them, with how many such reads each file has. A new file that
reads money, or a changed number of reads in a listed file, fails here: look
at the new read, apply counted() where it adds money up (or confirm it shows
one record, labelled void), then update the entry.

This runs in CI only -- nothing here runs when an order is cancelled.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATTERN = re.compile(
    r"\b(Payment|Invoice|InvoiceItem|CreditNote|CardDiscountClaim)\.objects"
    r"|\binvoices_for\(|\bcredit_notes_for\(|\bclaims_for\(|\.payments\.all\("
    r"|\bpayments_by_order\(|\bcredit_notes_by_order\("
)

# file -> (reads, what was decided)
REVIEWED = {
    "apps/discount/scoping.py": (2, "the scoping helpers themselves"),
    "apps/discount/services.py": (10, "card_usage counted(); the rest change one claim"),
    "apps/discount/views.py": (5, "redemptions + redeemed count counted(); one claim's detail"),
    "apps/discount/writes.py": (1, "one claim's decision"),
    "apps/portal/dashboard.py": (2, "station today + monthly sales counted()"),
    "apps/rental/api.py": (3, "one order / one invoice for the tablet; void invoice refused"),
    "apps/rental/approvals.py": (4, "credit-note lists (records, with void flag) and pending counts"),
    "apps/rental/credit_notes.py": (7, "one note at a time; refused on a cancelled order"),
    "apps/rental/live.py": (1, "KPIs exclude cancelled orders"),
    "apps/rental/manager_api.py": (2, "one card claim's decision"),
    "apps/rental/money.py": (1, "one order's deposit; callers blank it when cancelled"),
    "apps/rental/reports.py": (5, "collections, payments, share counted(); order JSON zeroed"),
    "apps/rental/requests.py": (12, "request records; billing zeroed when cancelled; cancel_settled locks one order"),
    "apps/rental/scoping.py": (4, "the scoping helpers themselves"),
    "apps/rental/services.py": (6, "writes, one-order reads; recent/customer invoices counted()"),
    "apps/rental/views.py": (7, "lists and one-record views, void labelled; KPIs settled only"),
    "apps/reports/common.py": (5, "payments, references, credit notes counted()"),
    "apps/reports/defs/cancelled_invoices.py": (2, "the ONE place cancelled orders' money is shown, on purpose"),
    "apps/reports/defs/consolidated.py": (2, "money only for COMPLETED orders"),
    "apps/reports/defs/credit_note.py": (1, "counted()"),
    "apps/reports/defs/customer_card.py": (1, "counted()"),
    "apps/reports/defs/customer_rent.py": (2, "invoices and notes counted()"),
    "apps/reports/defs/rent_details.py": (6, "totals counted(); a cancelled row's money blank"),
    "apps/reports/defs/rent_summary.py": (3, "invoices counted(); helpers counted()"),
}


def _reads():
    found = {}
    for folder in ("apps", "core", "theme"):
        for path in (ROOT / folder).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or "/migrations/" in rel:
                continue
            n = sum(1 for line in path.read_text().splitlines() if PATTERN.search(line))
            if n:
                found[rel] = n
    return found


def test_every_money_read_was_reviewed():
    found = _reads()
    new = sorted(set(found) - set(REVIEWED))
    gone = sorted(set(REVIEWED) - set(found))
    changed = sorted(f"{f}: {REVIEWED[f][0]} -> {found[f]}" for f in set(found) & set(REVIEWED)
                     if found[f] != REVIEWED[f][0])
    assert not (new or gone or changed), (
        "Money reads changed. A cancelled order's money must count nowhere (apps/rental/counted.py): "
        "check each read, then update REVIEWED.\n"
        f"new files: {new}\nno longer reading: {gone}\nchanged counts: {changed}"
    )
