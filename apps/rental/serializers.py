"""Type checks for the customer lookup API's `request_data`. Same pattern as
apps/fare/serializers.py and apps/fleet/serializers.py -- types only; the
lookup itself is apps/rental/services.py::customer_by_phone."""

from rest_framework import serializers

from core.api import text_field


class CustomerLookupRequest(serializers.Serializer):
    """mobile_no only -- the local number, no country code. The app sends
    what a station operator types or scans; mobile_country_code is not part
    of the match."""

    mobile_no = text_field(max_length=20)
