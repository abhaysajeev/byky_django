"""Type checks for the vehicles API's `request_data`. Same pattern as
apps/fare/serializers.py -- types only; which categories and vehicle types a
device may ask for is decided in apps/fleet/services.py::device_vehicles."""

from rest_framework import serializers

from apps.fleet.models import AssetCheckStatus, ServiceRequestStatus
from core.api import REQUIRED, date_field, datetime_field, uuid7_field, whole_number_field


class VehiclesRequest(serializers.Serializer):
    """Both optional: each only narrows the answer. The station is never sent --
    it is the session's."""

    vehicle_category_id = whole_number_field(required=False)
    vehicle_type_id = whole_number_field(required=False)


def _text(max_length=500):
    return serializers.CharField(
        max_length=max_length, trim_whitespace=True,
        error_messages={**REQUIRED, "blank": "is required", "max_length": f"must be at most {max_length} characters"},
    )


class _DatesMixin(serializers.Serializer):
    """Optional date range, company time, both days included."""

    from_date = date_field(required=False, allow_null=True)
    to_date = date_field(required=False, allow_null=True)

    def validate(self, values):
        start, end = values.get("from_date"), values.get("to_date")
        if start and end and start > end:
            raise serializers.ValidationError({"to_date": "must not be before from_date"})
        return values


# -- Service requests (apps/fleet/service_requests.py) --------------------------------


class AssetsRequest(serializers.Serializer):
    """Nothing to send: the station is the session's."""


class ServiceRequestCreateRequest(serializers.Serializer):
    """The tablet reports a vehicle or a station asset. `sync_id` is this
    call's id and becomes the request's id."""

    sync_id = uuid7_field()
    target = serializers.ChoiceField(choices=["vehicle", "asset"],
                                     error_messages={**REQUIRED, "invalid_choice": "must be vehicle or asset"})
    vehicle_id = whole_number_field(min_value=1, required=False, allow_null=True)
    asset_id = whole_number_field(min_value=1, required=False, allow_null=True)
    # From issue-types; and/or the operator's own words -- at least one.
    issue_type_id = whole_number_field(min_value=1, required=False, allow_null=True)
    issue = serializers.CharField(max_length=500, required=False, allow_blank=True, trim_whitespace=True,
                                  error_messages={"max_length": "must be at most 500 characters"})
    photo_id = serializers.UUIDField(required=False, allow_null=True, error_messages={"invalid": "must be a UUID"})
    requested_at = datetime_field()

    def validate(self, values):
        if not values.get("issue_type_id") and not (values.get("issue") or "").strip():
            raise serializers.ValidationError({"issue": "is required when no issue_type_id is sent"})
        # Exactly the id of what is reported.
        if values["target"] == "vehicle":
            if not values.get("vehicle_id"):
                raise serializers.ValidationError({"vehicle_id": "is required for a vehicle"})
            if values.get("asset_id"):
                raise serializers.ValidationError({"asset_id": "must be left out for a vehicle"})
        else:
            if not values.get("asset_id"):
                raise serializers.ValidationError({"asset_id": "is required for an asset"})
            if values.get("vehicle_id"):
                raise serializers.ValidationError({"vehicle_id": "must be left out for an asset"})
        return values


class ServiceRequestListRequest(_DatesMixin):
    """This station's service requests: optionally only this tablet's, one
    status, a date range."""

    status = serializers.ChoiceField(
        choices=ServiceRequestStatus.values, required=False, allow_blank=True,
        error_messages={"invalid_choice": "must be one of: pending, approved, rejected, completed"},
    )
    mine = serializers.BooleanField(required=False, default=False,
                                    error_messages={"invalid": "must be true or false"})
    page = whole_number_field(min_value=1, required=False, default=1)


class ServiceRequestDecisionRequest(serializers.Serializer):
    request_sync_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
    note = serializers.CharField(max_length=500, required=False, allow_blank=True,
                                 error_messages={"max_length": "must be at most 500 characters"})


# -- Image sharing (apps/fleet/image_shares.py) -----------------------------------------


class ImageShareCreateRequest(serializers.Serializer):
    """The tablet shares a photo (uploaded first, purpose image_share) with
    remarks. `sync_id` is this call's id and becomes the share's id."""

    sync_id = uuid7_field()
    photo_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
    remarks = _text()
    shared_at = datetime_field()


class ImageShareListRequest(_DatesMixin):
    mine = serializers.BooleanField(required=False, default=False,
                                    error_messages={"invalid": "must be true or false"})
    page = whole_number_field(min_value=1, required=False, default=1)


class ManagerImageShareListRequest(_DatesMixin):
    state_id = whole_number_field(min_value=1, required=False, allow_null=True)
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    page = whole_number_field(min_value=1, required=False, default=1)


# -- Issue types and asset checks (apps/fleet/asset_checks.py) -------------------------


class IssueTypesRequest(serializers.Serializer):
    """Optional: only the types for a vehicle or for an asset (each includes
    the ones for both)."""

    target = serializers.ChoiceField(choices=["vehicle", "asset"], required=False, allow_blank=True,
                                     error_messages={"invalid_choice": "must be vehicle or asset"})


class IssueTypeCreateRequest(serializers.Serializer):
    """A new issue type from the tablet, to pick at once (owner, 29 Oct 2026)."""

    name = serializers.CharField(max_length=100, error_messages={
        "required": "is required", "blank": "is required", "max_length": "must be at most 100 characters"})
    applies_to = serializers.ChoiceField(choices=["vehicle", "asset", "both"], error_messages={
        "required": "is required", "invalid_choice": "must be vehicle, asset or both"})
    description = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    # Vehicle issue types only: a manager's approval of such a request takes
    # the vehicle off rent until it is back in service.
    needs_warehouse_service = serializers.BooleanField(required=False, default=False)

    def validate(self, values):
        values["name"] = " ".join(values["name"].split())
        if values.get("needs_warehouse_service") and values["applies_to"] != "vehicle":
            raise serializers.ValidationError(
                {"needs_warehouse_service": "is for vehicle issue types only (applies_to vehicle)"})
        return values


class AssetCheckCreateRequest(serializers.Serializer):
    """The tablet checks an asset of its station: photo (uploaded first,
    purpose asset_check) and condition; remarks optional. `sync_id` is this
    call's id and becomes the check's id."""

    sync_id = uuid7_field()
    asset_id = whole_number_field(min_value=1)
    status = serializers.ChoiceField(
        choices=AssetCheckStatus.values,
        error_messages={**REQUIRED, "invalid_choice": "must be active or needs_inspection"})
    photo_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
    remarks = serializers.CharField(max_length=500, required=False, allow_blank=True, trim_whitespace=True,
                                    error_messages={"max_length": "must be at most 500 characters"})
    checked_at = datetime_field()


class AssetCheckListRequest(_DatesMixin):
    status = serializers.ChoiceField(choices=AssetCheckStatus.values, required=False, allow_blank=True,
                                     error_messages={"invalid_choice": "must be active or needs_inspection"})
    mine = serializers.BooleanField(required=False, default=False,
                                    error_messages={"invalid": "must be true or false"})
    page = whole_number_field(min_value=1, required=False, default=1)


class ManagerAssetCheckListRequest(_DatesMixin):
    state_id = whole_number_field(min_value=1, required=False, allow_null=True)
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    asset_id = whole_number_field(min_value=1, required=False, allow_null=True)
    status = serializers.ChoiceField(choices=AssetCheckStatus.values, required=False, allow_blank=True,
                                     error_messages={"invalid_choice": "must be active or needs_inspection"})
    page = whole_number_field(min_value=1, required=False, default=1)


class AssetCheckDetailRequest(serializers.Serializer):
    asset_check_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
