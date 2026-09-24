"""Range builders for the fare exclusion constraints.

Their own module so migrations import something stable rather than models.py.
"""

from django.contrib.postgres.fields import DateRangeField, IntegerRangeField
from django.db.models import Func


class DateRangeFunc(Func):
    """daterange(start, end, bounds) -- a fare, season or branch link's dates."""

    function = "DATERANGE"
    output_field = DateRangeField()


class IntRangeFunc(Func):
    """int4range(start, end) -- a rule's window in minutes, [start, end)."""

    function = "INT4RANGE"
    output_field = IntegerRangeField()
