"""Dial codes for mobile numbers (owner, 11 Nov 2026): for each country its
name, flag, dialling code and how many digits a mobile number has after the
code -- what the apps' phone field offers.

Where the data comes from, once, by hand (`manage.py load_dial_codes
--rebuild`):

- Google's libphonenumber data (the `phonenumbers` package, Apache-2.0):
  every region's dialling code and its mobile number lengths;
- Unicode CLDR (the `babel` package, BSD): each region's English name.
  libphonenumber names some regions only through a number, and a shared code
  (+39 Italy / Vatican, +61 Australia / Christmas Island) gives no name.

The result is DATA_FILE, kept in the repository, and the database is filled
from that file -- so the server needs neither package and never calls out.
Both are pinned in requirements/dev.txt for the rebuild and its test.
"""

import json
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "dial_codes.json"

# Offered first, in this order: the UAE, then the rest of the GCC. Everyone
# else after, A-Z. sort_order can be changed per row later; a reload never
# overwrites it.
FIRST = ("AE", "SA", "KW", "QA", "BH", "OM")
OTHERS = 100


def flag(iso2):
    """The flag emoji: the two regional-indicator letters of the ISO code."""
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in iso2.upper())


def default_sort(iso2):
    return FIRST.index(iso2) + 1 if iso2 in FIRST else OTHERS


def build():
    """Every region libphonenumber knows, from the packages (rebuild only)."""
    import phonenumbers
    from babel import Locale
    from phonenumbers import PhoneMetadata, PhoneNumberType

    names = Locale("en").territories
    rows = []
    for iso2 in sorted(phonenumbers.SUPPORTED_REGIONS):
        meta = PhoneMetadata.metadata_for_region(iso2)
        lengths = [n for n in (meta.mobile.possible_length or ()) if n > 0] if meta.mobile else []
        if not lengths:                       # a region with no mobile range: its numbers' lengths
            lengths = [n for n in (meta.general_desc.possible_length or ()) if n > 0]
        example = phonenumbers.example_number_for_type(iso2, PhoneNumberType.MOBILE)
        rows.append({
            "iso2": iso2,
            "country": names.get(iso2) or iso2,
            "dial_code": str(phonenumbers.country_code_for_region(iso2)),
            "mobile_digits_min": min(lengths),
            "mobile_digits_max": max(lengths),
            "example_mobile": phonenumbers.national_significant_number(example) if example else "",
        })
    return rows


def write_file(rows, path=DATA_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def read_file(path=DATA_FILE):
    return json.loads(path.read_text(encoding="utf-8"))


def load(model, rows):
    """Write `rows` into the DialCode table (`model`, so a migration can pass
    its historical model). Re-runnable: a row is matched by iso2; is_active
    and sort_order are set on a new row only, so a person's choices survive
    a reload. Returns (created, updated)."""
    created = updated = 0
    for row in rows:
        values = {
            "country": row["country"], "flag": flag(row["iso2"]), "dial_code": row["dial_code"],
            "dial_code_digits": len(row["dial_code"]), "mobile_digits_min": row["mobile_digits_min"],
            "mobile_digits_max": row["mobile_digits_max"], "example_mobile": row.get("example_mobile", ""),
        }
        existing = model.objects.filter(iso2=row["iso2"]).first()
        if existing is None:
            model.objects.create(iso2=row["iso2"], is_active=True, sort_order=default_sort(row["iso2"]), **values)
            created += 1
        elif any(getattr(existing, key) != value for key, value in values.items()):
            for key, value in values.items():
                setattr(existing, key, value)
            existing.save()
            updated += 1
    return created, updated


def device_dial_codes(model):
    """What the apps get: active rows, in order, and when the list last changed."""
    rows = list(model.objects.filter(is_active=True).order_by("sort_order", "country"))
    newest = max((r.modified_on or r.created_on for r in rows), default=None)
    return {
        "version": newest.isoformat() if newest else None,
        "dial_codes": [
            {"iso2": r.iso2, "country": r.country, "flag": r.flag, "dial_code": f"+{r.dial_code}",
             "dial_code_digits": r.dial_code_digits, "mobile_digits_min": r.mobile_digits_min,
             "mobile_digits_max": r.mobile_digits_max, "example": r.example_mobile}
            for r in rows
        ],
    }
