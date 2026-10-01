"""How a list screen orders its rows: the record saved last comes first.

Every editable list (masters, customers, users, fares, ...) puts the row
someone just added or changed at the top, so after Save it is right there.
Applied per list view, on the queryset it renders -- not on Meta.ordering or
the scoping helpers, which also feed dropdowns and device APIs that stay
alphabetical or date-ordered.

modified_on is set on every save (TimeStampedModel, auto_now) but is NULL on
rows imported without one, so those sort last rather than first (Postgres puts
NULLs first on DESC); created_on then pk break ties.
"""

from django.db.models import F


def recent_first(queryset):
    return queryset.order_by(F("modified_on").desc(nulls_last=True), F("created_on").desc(), "-pk")


def recent_key(row):
    """The same order for rows built in Python: sort with
    `sorted(rows, key=recent_key)` where each row has modified_on/created_on
    (either may be None)."""
    modified, created = getattr(row, "modified_on", None), getattr(row, "created_on", None)
    stamp = modified or created
    return (stamp is None, -stamp.timestamp() if stamp else 0)
