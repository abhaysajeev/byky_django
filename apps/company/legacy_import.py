"""Loading the client's live masters.

Reads the JSON that `tools/extract_live_masters.py` writes from
`Byky Live Data.xls`, and writes `company.Country` and `company.State`.

Four decisions taken with the client, all visible in the code below:

**Legacy ids are preserved.** `CountryID` and `StateID` become the primary key,
so `CoreLocation.StateID` and `CoreBranch.LocationID` still point at the right
rows when those tables are imported. The alternative -- new ids plus a mapping
every later import has to read -- puts the same decision in four more places.

**Values are imported verbatim.** `short_code` therefore holds the dialling code
(`971`), and a state's code is its own id as text (`1`..`7`). Both are what the
legacy holds; neither is invented here. Cleaning them up is a separate,
reviewable change if the client wants one.

**UAE only.** Kuwait (`CountryID 2`, one branch at Mangaf) is out of scope for
the pilot. It is skipped rather than filtered out silently: the command reports
every row it left behind.

**Legacy timestamps are kept.** `TimeStampedModel` uses `auto_now_add` /
`auto_now`, which would stamp every row with the time of the import and discard
dates going back to 2017. A queryset `.update()` does not fire those, so the
real `CreatedOn` / `ModifiedOn` are written straight after the insert.

`CreatedBy` / `ModifiedBy` are legacy user ids (1, 24) with no counterpart here
yet, so they land NULL. When users are imported they can be backfilled from the
same export.
"""

import base64
import binascii
import datetime
import json
import pathlib
import zoneinfo

from django.db import connection

from core.enums import ApprovalStatus

DATA = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "legacy"

# The pilot is UAE. Kuwait exists in the source and is deliberately left out:
# one state (Mangaf), one location and one branch (Hilton Kuwait Resort).
COUNTRIES_IN_SCOPE = {1}

# The legacy has one. Multi-company is a real requirement, so the import is
# written as a set rather than assuming there can only ever be this one.
COMPANIES_IN_SCOPE = {1}

# CoreBranchType, read from the live QA database: 1 Head Office, 2 Depot,
# 3 Station. All 97 live branches are Stations.
BRANCH_TYPES = {1: "head_office", 2: "depot", 3: "station"}

# The legacy stores naive local time and converts with an ApplicationConfig
# offset. The system ran in the UAE, so the wall-clock times in the export are
# Gulf Standard Time; they are read as that and stored as UTC, which is what
# every timestamp in this database means (design/00-findings.md section 7).
#
# Note the landmine in CLAUDE.md section 9: ApplicationConfig.MinutesFromUTC is
# 320, which is not the UAE's +240. Nothing in these two tables depends on the
# minute, so +04:00 is used and the discrepancy is left for the transactional
# tables, where it will matter.
LEGACY_ZONE = zoneinfo.ZoneInfo("Asia/Dubai")


def load(filename):
    return json.loads((DATA / filename).read_text())


def moment(value):
    """A legacy `datetime` string as an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    for shape in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.datetime.strptime(text, shape)
        except ValueError:
            continue
        return naive.replace(tzinfo=LEGACY_ZONE).astimezone(datetime.UTC)
    raise ValueError(f"Unrecognised legacy timestamp: {value!r}")


def approval(row):
    """`IsApproved` / `IsActive` as the three fields that replaced them.

    `want_approval` has no legacy source -- it is the new "does this row need a
    second pair of eyes" switch -- and a row that is already live does not.
    """
    return {
        "want_approval": False,
        "approval_status": ApprovalStatus.APPROVED if row["IsApproved"] else ApprovalStatus.PENDING,
        "is_active": bool(row["IsActive"]),
    }


def text(value):
    """Excel reads codes as numbers; the column is `nvarchar`."""
    return "" if value is None else str(value).strip()


def decoded(value):
    """`CoreCompany.Name` is base64 in the live database.

    Only that one column: the address beside it is plain text. Decoded here
    rather than left for someone to notice, because the row reads as gibberish
    otherwise -- `QlkgS1kgU1BPUlQ...` is `BY KY SPORT & LEISURE EQUIPMENT
    RENTAL & TRADING LLC`.

    A value that is not base64, or that does not decode to text, is kept as it
    is: better a name that looks odd than one this function invented.
    """
    raw = text(value)
    try:
        decoded_bytes = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return raw
    try:
        out = decoded_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        return raw
    return out or raw


def day(value):
    """A legacy `datetime` as a date. Times of day on these columns are 00:00."""
    moment_ = moment(value)
    return moment_.date() if moment_ else None


def flag(value):
    """A legacy `tinyint`/`int` flag. NULL means the feature was never set,
    which is the same as off (`IsTestVehicle` is NULL on 29 branches)."""
    return bool(value)


def countries():
    """[(pk, defaults, created_on, modified_on)] for the countries in scope."""
    out = []
    for row in load("core_country.json"):
        if row["CountryID"] not in COUNTRIES_IN_SCOPE:
            continue
        out.append((
            row["CountryID"],
            {"short_code": text(row["ShortCode"]), "name": text(row["Name"]), **approval(row)},
            moment(row["CreatedOn"]),
            moment(row["ModifiedOn"]),
        ))
    return out


def states():
    out = []
    for row in load("core_state.json"):
        if row["CountryID"] not in COUNTRIES_IN_SCOPE:
            continue
        out.append((
            row["StateID"],
            {
                "country_id": row["CountryID"],
                "short_code": text(row["ShortCode"]),
                "name": text(row["Name"]),
                **approval(row),
            },
            moment(row["CreatedOn"]),
            moment(row["ModifiedOn"]),
        ))
    return out


def companies():
    out = []
    for row in load("core_company.json"):
        if row["CompanyID"] not in COMPANIES_IN_SCOPE:
            continue
        out.append((
            row["CompanyID"],
            {
                "short_code": text(row["ShortCode"]),
                "name": decoded(row["Name"]),
                "country_id": row["CountryID"],
                "state_id": row["StateID"],
                "address": text(row["Address"]),
                "city": text(row["City"]),
                "zip_code": text(row["Zip"]),
                "phone_number": text(row["PhoneNumber"]),
                "fax_number": text(row["FaxNumber"]),
                # NULL in the live row. The field is required on the form, not
                # in the database, so an empty string is honest -- inventing an
                # address would not be.
                "email": text(row["Email"]),
                # Verbatim, so "www.q8byky.com" keeps its missing scheme. The
                # URL validator only runs on the form, and correcting data on
                # the way in is how an import starts disagreeing with its source.
                "website": text(row["Website"]),
                "ceo_name": text(row["CEOName"]),
                "dto_name": text(row["DTOName"]),
                "contact_person": text(row["ContactPerson"]),
                "date_of_commissioning": day(row["DateOfCommissioning"]),
                "incorporation_certificate_number": text(row["IncorporationCertificateNumber"]),
                "business_certificate_number": text(row["BusinessCertificateNumber"]),
                "tax_percentage": row["TaxPercentage"] or 0,
                "income_tax_number": text(row["IncomeTaxNumber"]),
                "service_tax_number": text(row["ServiceTaxNumber"]),
                "tin": text(row["TINNumber"]),
                "cst": text(row["CSTNumber"]),
                **approval(row),
            },
            moment(row["CreatedOn"]),
            moment(row["ModifiedOn"]),
        ))
    return out


def _states_in_scope():
    return {pk for pk, *_ in states()}


def locations():
    wanted = _states_in_scope()
    out = []
    for row in load("core_location.json"):
        if row["StateID"] not in wanted:
            continue
        out.append((
            row["LocationID"],
            {
                "country_id": row["CountryID"],
                "state_id": row["StateID"],
                "short_code": text(row["ShortCode"]),
                "name": text(row["Name"]),
                "landmark": text(row["LandMark"]),
                **approval(row),
            },
            moment(row["CreatedOn"]),
            moment(row["ModifiedOn"]),
        ))
    return out


def branches():
    """Branches of an imported company, at an imported location.

    `station_number`, `address`, `latitude`, `longitude` and `contact_no` are
    left empty: they live in `CmsStationAddressMapping` (85 rows), which is not
    in this workbook. `departments` likewise -- `CoreBranchDepartments` is a
    separate table, and Department has not been imported.
    """
    wanted = {pk for pk, *_ in locations()}
    out = []
    for row in load("core_branch.json"):
        if row["LocationID"] not in wanted or row["CompanyID"] not in COMPANIES_IN_SCOPE:
            continue
        out.append((
            row["BranchID"],
            {
                "company_id": row["CompanyID"],
                "location_id": row["LocationID"],
                "short_code": text(row["ShortCode"]),
                "name": text(row["Name"]),
                "branch_type": BRANCH_TYPES[row["BranchTypeID"]],
                "is_multi_device": flag(row["IsMultipleUser"]),
                "allows_test_ride": flag(row["IsTestVehicle"]),
                "is_hotel": flag(row["IsHotel"]),
                "accepts_app_payment": flag(row["IsAppPayment"]),
                "hotel_commission": row["HotelCommission"] or 0,
                **approval(row),
            },
            moment(row["CreatedOn"]),
            moment(row["ModifiedOn"]),
        ))
    return out


def skipped():
    """What was left out, so a run can say so rather than quietly drop rows."""
    out = []
    for row in load("core_country.json"):
        if row["CountryID"] not in COUNTRIES_IN_SCOPE:
            out.append(f"country {row['CountryID']} {row['Name']} (out of scope)")
    for row in load("core_state.json"):
        if row["CountryID"] not in COUNTRIES_IN_SCOPE:
            out.append(f"state {row['StateID']} {row['Name']} (country out of scope)")
    return out


def skipped_structure():
    """What the company/location/branch import left behind."""
    wanted_states = _states_in_scope()
    out = []
    for row in load("core_company.json"):
        if row["CompanyID"] not in COMPANIES_IN_SCOPE:
            out.append(f"company {row['CompanyID']} (out of scope)")
    dropped = set()
    for row in load("core_location.json"):
        if row["StateID"] not in wanted_states:
            dropped.add(row["LocationID"])
            out.append(f"location {row['LocationID']} {row['Name']} (state out of scope)")
    for row in load("core_branch.json"):
        if row["LocationID"] in dropped:
            out.append(f"branch {row['BranchID']} {row['Name']} (location out of scope)")
    return out


def reset_sequence(model):
    """Move the table's id counter past the ids just written.

    Postgres keeps a counter per serial column, and it only moves when *it*
    supplies the id. These imports write the legacy's own ids, so the counter is
    left at 1 while the table holds id 97 -- and the next row someone adds
    through a screen fails with "duplicate key". It is invisible until a person
    tries to add a station, which is the worst time to find it.
    """
    table = model._meta.db_table
    column = model._meta.pk.column
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT setval(pg_get_serial_sequence(%s, %s), '
            f'COALESCE((SELECT MAX("{column}") FROM "{table}"), 1))',
            [table, column],
        )


def write(model, rows):
    """Insert or update by primary key, then restore the legacy timestamps.

    The second step is a queryset `.update()` on purpose: `Model.save()` would
    fire `auto_now_add`/`auto_now` and overwrite them with now.
    """
    created = updated = 0
    for pk, defaults, created_on, modified_on in rows:
        _, was_created = model.objects.update_or_create(pk=pk, defaults=defaults)
        model.objects.filter(pk=pk).update(created_on=created_on, modified_on=modified_on)
        created, updated = created + was_created, updated + (not was_created)
    reset_sequence(model)
    return created, updated


# The two rows created by hand while the screens were being built, and the real
# legacy row each was standing in for. Anything pointing at a stand-in is moved
# to the real row before the stand-in is deleted, so no reference is lost and
# nothing is guessed: "AE" was the UAE, "AUH" was Abu Dhabi.
#
# The company's registered address therefore lands on Abu Dhabi. The legacy has
# it in Dubai; that correction belongs to the Company import, which is where
# the company's own row is read.
REPLACEMENTS = {
    "country": {"AE": 1},
    "state": {"AUH": 3},
    # The company row created during the build carries the real short code but
    # the wrong id: the legacy has BYKY at CompanyID 1, and CoreBranch.CompanyID
    # points there.
    "company": {"BYKY": 1},
}


def park(instance):
    """Move a stand-in's unique values out of the way of the incoming row.

    The build-time company holds `short_code = "BYKY"`, which is unique across
    the table -- and the live company has the same code at a different id. So
    the insert collides before the stand-in can be dealt with, and the stand-in
    cannot be deleted first because its references have nowhere to go until the
    live row exists.

    Parking breaks the deadlock: the value is prefixed and made unique by the
    row's own id, the live row is written, references move, and the parked row
    is deleted. The parked value only ever exists inside the import's
    transaction.
    """
    parked = {}
    for field in instance._meta.fields:
        if not field.unique or field.primary_key or not hasattr(field, "max_length"):
            continue
        value = getattr(instance, field.attname)
        if not isinstance(value, str) or not value:
            continue
        marker = f"~{instance.pk}"
        parked[field.attname] = (marker + value)[: field.max_length or len(value)]
    if parked:
        type(instance).objects.filter(pk=instance.pk).update(**parked)
    return list(parked)


def replacement_for(instance):
    """The legacy row that replaces this build-time one, or None."""
    return REPLACEMENTS[instance._meta.model_name].get(instance.short_code)


def repoint(instance, new_pk):
    """Move every reference to `instance` onto `new_pk`.

    Walks the model's own relations rather than naming the tables, so a column
    added later cannot be forgotten here. `include_hidden` matters: Company's
    country and state are declared `related_name="+"`, which keeps them out of
    the ordinary reverse-relation list.
    """
    moved = []
    for relation in instance._meta.get_fields(include_hidden=True):
        if not relation.is_relation or not relation.auto_created or relation.concrete:
            continue
        model, field = relation.related_model, relation.field.name
        count = model.objects.filter(**{field: instance}).update(**{f"{field}_id": new_pk})
        if count:
            moved.append(f"{count} x {model._meta.verbose_name_plural}.{field}")
    return moved


def stand_ins(model, rows):
    """Rows in this table that the live export does not account for.

    These are the ones created by hand while the screens were being built -- a
    stand-in UAE and Abu Dhabi. Each is classified by what the import needs to
    do with its primary key:

    * **displaced** -- the id belongs to a *different* live row. `State 2` is
      Abu Dhabi here and Sharjah in the legacy, so writing the import over it
      would silently turn every reference to Abu Dhabi into a reference to
      Sharjah. Its references are moved first, then the row is overwritten.
    * **orphan** -- the id is not used by the import at all, so the row is moved
      and deleted.

    Identified by comparing content, not by name: a row whose id and short code
    already match the export is a previous run of this import, not a stand-in.
    """
    wanted = {pk: defaults for pk, defaults, *_ in rows}
    out = []
    for existing in model.objects.all():
        target = wanted.get(existing.pk)
        if target and target["short_code"] == existing.short_code:
            continue                      # already imported
        out.append((existing, "displaced" if target else "orphan"))
    return out
