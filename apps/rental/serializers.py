"""Type checks for the customer APIs' `request_data`. Same pattern as
apps/fare/serializers.py and apps/fleet/serializers.py -- types only; the
lookup and the create themselves are apps/rental/services.py."""

from rest_framework import serializers

from apps.rental.models import Gender, IdType, full_number
from core.api import REQUIRED, date_field, text_field, uuid7_field


class PhoneFields(serializers.Serializer):
    """The phone, three ways, all required: the app sends the full number it
    built so the server can match on it directly, and the two parts so the
    customer's record keeps them -- and the three must agree."""

    mobile_country_code = text_field(max_length=10)
    mobile_no = text_field(max_length=20)
    full_number = serializers.RegexField(
        r"^\d+$", max_length=30,
        error_messages={**REQUIRED, "invalid": "must be digits only",
                        "max_length": "must be at most 30 characters"},
    )

    def validate(self, values):
        if values["full_number"] != full_number(values["mobile_country_code"], values["mobile_no"]):
            raise serializers.ValidationError(
                {"full_number": "does not match mobile_country_code and mobile_no"}
            )
        return values


class CustomerLookupRequest(PhoneFields):
    """Matched on full_number."""


def _optional_text(max_length=None):
    if max_length is None:
        return serializers.CharField(required=False, allow_blank=True,
                                     error_messages={"invalid": "must be text"})
    return text_field(max_length=max_length, required=False, allow_blank=True)


def _choice(choices, names):
    return serializers.ChoiceField(
        choices=choices, required=False, allow_blank=True,
        error_messages={"invalid_choice": f"must be one of {names}"},
    )


class CustomerCreateRequest(PhoneFields):
    """A new customer from the operator app. Only first_name and the phone
    are required -- a walk-in is registered before their document is seen."""

    sync_id = uuid7_field()
    first_name = text_field(max_length=100)
    last_name = _optional_text(100)
    gender = _choice(Gender.choices, ", ".join(Gender.values))
    date_of_birth = date_field(required=False, allow_null=True)
    nationality = _optional_text(100)
    id_type = _choice(IdType.choices, ", ".join(IdType.values))
    id_no = _optional_text(100)
    email = serializers.EmailField(required=False, allow_blank=True,
                                   error_messages={"invalid": "must be a valid email"})
    address = _optional_text()
    remarks = _optional_text()
