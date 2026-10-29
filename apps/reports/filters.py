"""The report filters, read from the query string -- the same string drives the
page, its JSON and its export, so a link, a refresh and a download all show
the same rows.

A dropdown left on "All" sends nothing. A value that is not a whole number
(or not one of a status filter's choices) is ignored, as "All" -- a report
never fails on a hand-edited URL. The date-time range is company local time:
`from` and `to` are `YYYY-MM-DDTHH:MM` (the browser's datetime-local), `to`
includes its whole minute; left out, today 00:00 to 23:59.
"""

import datetime

ID_FILTERS = {
    "state": "State",
    "branch": "Branch",
    "cashier": "Cashier",
    "category": "Category",
    "vehicle_type": "Vehicle Type",
    "card_type": "Card Type",
    "card_grade": "Card Grade",
    "asset_type": "Asset Type",
}
TEXT_FILTERS = {
    "customer": "Customer Name",
    "phone": "Phone No",
    "order_no": "Order No",
    "vehicle_no": "Vehicle No",
    "credit_note_no": "Credit Note No",
    "device": "Device Name",
    "rfid": "RFID Tag No",
    "card_no": "Card Number",
    "asset": "Asset No",
}
CHOICE_FILTERS = {"status": "Status"}
LABELS = {**ID_FILTERS, **TEXT_FILTERS, **CHOICE_FILTERS}
INPUT_FORMAT = "%Y-%m-%dT%H:%M"
SHOWN_FORMAT = "%d/%m/%Y %I:%M %p"


def _whole(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def _moment(value):
    try:
        return datetime.datetime.strptime((value or "").strip(), INPUT_FORMAT)
    except ValueError:
        return None


class Filters:
    """One request's filters for one report: `.get(key)`, `.since` / `.until`
    (aware, [since, until)), and the values the bar shows back."""

    def __init__(self, params, report, zone, now):
        self.report = report
        self.zone = zone
        self.values = {}
        for key in report.filters:
            raw = params.get(key, "")
            if key in ID_FILTERS:
                self.values[key] = _whole(raw)
            elif key in TEXT_FILTERS:
                self.values[key] = raw.strip()[:60]
            elif key in CHOICE_FILTERS:
                self.values[key] = raw if raw in dict(report.statuses) else ""
        today = now.astimezone(zone).date()
        start = _moment(params.get("from")) or datetime.datetime.combine(today, datetime.time.min)
        end = _moment(params.get("to")) or datetime.datetime.combine(today, datetime.time(23, 59))
        if end < start:
            start, end = end, start
        self.start, self.end = start, end            # naive, company time -- as shown
        self.since = start.replace(tzinfo=zone)
        self.until = (end + datetime.timedelta(minutes=1)).replace(tzinfo=zone)

    def get(self, key):
        return self.values.get(key) or None

    @property
    def any_vehicle(self):
        """A filter that picks vehicles (Rent Details shows only those lines)."""
        return bool(self.get("category") or self.get("vehicle_type") or self.get("vehicle_no"))

    def query(self):
        """The query string these filters came from, normalised."""
        pairs = []
        if self.report.dated:
            pairs += [("from", self.start.strftime(INPUT_FORMAT)), ("to", self.end.strftime(INPUT_FORMAT))]
        pairs += [(k, str(v)) for k, v in self.values.items() if v]
        return pairs

    def inputs(self):
        """What the filter bar shows back: datetime-local values and the rest."""
        return {"from": self.start.strftime(INPUT_FORMAT), "to": self.end.strftime(INPUT_FORMAT),
                **{k: ("" if v is None else str(v)) for k, v in self.values.items()}}

    def describe(self, names):
        """'From : … To : …' and the applied filters, by name -- the Excel
        header's second row and the page's scope line. `names[key][id]` gives a
        dropdown value's name."""
        parts = []
        for key, value in self.values.items():
            if not value:
                continue
            if key in ID_FILTERS:
                value = names.get(key, {}).get(value, value)
            elif key in CHOICE_FILTERS:
                value = dict(self.report.statuses).get(value, value)
            parts.append(f"{LABELS[key]} : {value}")
        return parts

    def period(self):
        return f"From : {self.start.strftime(SHOWN_FORMAT)}    To : {self.end.strftime(SHOWN_FORMAT)}"
