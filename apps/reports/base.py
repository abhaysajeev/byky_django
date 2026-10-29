"""What every report is made of, and the one result the page, its JSON and
its Excel export are all drawn from -- so the three can never disagree.

A report (apps/reports/defs/) declares its filters and columns and builds a
`Result`: the rows on screen (data rows, and the TOTAL rows of its blocks),
the grand total, a few headline figures and how many units the filters
found. Rows carry typed values (Decimal, datetime); the JSON turns them into
display strings and the Excel writer writes them as numbers and dates.
"""

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0.000")

# Column kinds -- how a value is drawn and summed.
TEXT, MONEY, COUNT, PERCENT, DATETIME, DATE = "text", "money", "count", "percent", "datetime", "date"

# A row's place in the table.
DATA, TOTAL, GRAND = "data", "total", "grand"


@dataclass(frozen=True)
class Column:
    key: str
    label: str                 # the client's own column name, exactly (Excel header)
    kind: str = TEXT
    part: str = "row"          # Rent Details: "order" (once per order) or "line" (per vehicle)
    summed: bool | None = None  # in TOTAL rows; money and counts by default
    width: int = 16            # Excel column width

    @property
    def sums(self):
        return self.summed if self.summed is not None else self.kind in (MONEY, COUNT)

    @property
    def numeric(self):
        return self.kind in (MONEY, COUNT, PERCENT)


@dataclass
class Row:
    cells: dict
    kind: str = DATA
    lines: list | None = None   # per-vehicle cells, when the report has a "line" part
    label: str = ""             # TOTAL / GRAND TOTAL / a block's name


@dataclass
class Kpi:
    label: str
    value: object
    kind: str = MONEY


@dataclass
class Result:
    columns: list
    rows: list                  # the rows to draw (one page, or everything for an export)
    total: dict | None          # grand total cells; None when the report has no totals
    kpis: list = field(default_factory=list)
    count: int = 0              # units found (orders, rows, vehicles...)
    pages: int = 1
    page: int = 1
    first: int = 0              # 1-based index of the first paged item on this page (0 when none)
    last: int = 0
    size: int = 0               # paged items in all (units; rows with their TOTALs in a block report)
    note: str = ""              # e.g. the vehicle-filter rule
    blocks: bool = False        # rows come in blocks with their own TOTAL rows


class Report:
    """One report. Subclasses set the class attributes and `build`."""

    slug = ""
    title = ""                  # on screen and in the sidebar
    sheet_title = ""            # the Excel title, as the client named it
    page_code = ""
    filters = ()                # filter keys, in the order the bar shows them (apps/reports/filters.py)
    statuses = ()               # (value, label) for a `status` filter
    unit = ("row", "rows")
    empty = "Nothing found for these filters."
    dated = True                # has a date-time range
    description = ""            # the grey line under the title

    def build(self, ctx, page=None, per_page=None):
        """A Result: `page` of `per_page` units, or every unit when page is None."""
        raise NotImplementedError

    def screen_columns(self, columns):
        """The columns in the order the screen draws them. The export keeps
        the client's order; a report may move columns forward on screen."""
        return columns


def total_of(rows, columns):
    """Sum the summed columns over data rows -- Decimal for money, int for counts."""
    out = {}
    for column in columns:
        if not column.sums:
            continue
        values = [r.cells.get(column.key) for r in rows if r.kind == DATA]
        values = [v for v in values if v is not None]
        out[column.key] = sum(values, ZERO if column.kind == MONEY else 0)
    return out


def window(size, page, per_page):
    """(page, pages, start, stop) -- the slice of `size` items one page shows;
    everything when page is None (an export)."""
    if page is None or not per_page:
        return 1, 1, 0, size
    pages = max((size + per_page - 1) // per_page, 1)
    page = min(max(page, 1), pages)
    start = (page - 1) * per_page
    return page, pages, start, min(start + per_page, size)


def paged(rows, page, per_page, **fields):
    """A Result over an in-memory list of rows: the page's slice, and the pager."""
    page, pages, start, stop = window(len(rows), page, per_page)
    return Result(rows=rows[start:stop], page=page, pages=pages, first=start + 1 if stop > start else 0,
                  last=stop, size=len(rows), **fields)


def local(moment, zone):
    """A stored (UTC) moment in the company's time, naive -- for display and Excel."""
    return moment.astimezone(zone).replace(tzinfo=None) if moment else None
