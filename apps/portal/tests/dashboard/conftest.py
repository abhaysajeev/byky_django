"""The dashboard reads orders, invoices, devices and attendance, so its tests
borrow the rental tests' world: two companies, a signed-in administrator and
a station with a tablet, two bikes and a customer. Kept in this folder so the
other portal tests keep their own `user`."""

from apps.rental.tests.conftest import client_in, user, world  # noqa: F401
from apps.rental.tests.test_order_screens import shop  # noqa: F401
