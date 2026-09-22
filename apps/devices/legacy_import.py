"""Loading the client's live device settings.

Reads what `tools/extract_device_settings.py` writes from `Byky Live Data.xls`
-- `data/legacy/sfa_device_setting.json` and the bitmaps beside it -- and writes
`devices.DeviceSettings`. 95 stations: their receipt wording, their printer's
habits, their order-number prefix and their logo.

Decisions, all visible below:

**The workbook is the source, not QA.** QA holds 82 of these rows and is missing
thirteen stations; 34 of the rows it does hold carry an older logo. Checked
before choosing, not assumed.

**Legacy ids are preserved**, as every other import here does: `DeviceSettingID`
becomes the primary key, and `BranchID` already is `Branch.pk`.

**The logo is imported as bytes, unchanged.** The legacy kept it base64 in the
row; these are 1-bit bitmaps 832 dots wide -- the print head's own width -- so
anything that re-encoded them would change what a station prints. They go into
the row here for the same reason the legacy put them there: the tablet is sent
the picture itself with its settings and prints from it offline.

**Legacy timestamps are kept**, including `LogoChangedOn`: a tablet re-downloads
the logo only when that moment moves, so importing it as "now" would make every
tablet in the network fetch a logo it already has.

**The dropped columns are design/03-login.md section 8.7's list**, not a choice
made here. `TaxType` is the company's; `Discount`, `MinSaleQty`, `SaleType`,
`GPSTimeInterval` and the rest of the van-sales group were never sent to a
tablet. `PreviewLogo` is the same picture again.

`CreatedBy` / `ModifiedBy` are legacy user ids (1, 24) with no counterpart here
yet, so they land NULL, exactly as the company import leaves them.
"""

import json
import pathlib
from decimal import Decimal

from apps.company.legacy_import import approval, moment, text
from apps.devices.models import PrintType, RoundOffMode

DATA = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "legacy"
SOURCE = "sfa_device_setting.json"
LOGOS = DATA / "device_logos"

# The legacy's PrintTypes enum has one member: Landscape = 1 (Portrait is
# commented out in ERP.Utils/Enumeration.cs:334), and every live row is 1.
PRINT_TYPES = {0: PrintType.PORTRAIT, 1: PrintType.LANDSCAPE}

# RoundOff 0/1/2, where the legacy called 0 "Auto".
ROUND_OFF_MODES = {0: RoundOffMode.NEAREST, 1: RoundOffMode.UPWARD, 2: RoundOffMode.DOWNWARD}

# RoundOffLimit 0/1/2 became the amount itself (ERP.Interface/SFA.cs:910-914).
ROUND_OFF_STEPS = {0: Decimal("0.25"), 1: Decimal("0.50"), 2: Decimal("1.00")}

# The tablet proc's own fallback when a slot is NULL
# (Service_Get_Device_SEttings.sql: ISNULL(TestTimeSlot, 5)).
DEFAULT_TEST_MINUTES = 5


def load():
    return json.loads((DATA / SOURCE).read_text())


def logo_bytes(row):
    """The bitmap for a row, or b"" -- read from the file the extractor wrote,
    never re-encoded on the way through."""
    name = row.get("LogoFile")
    if not name:
        return b""
    path = LOGOS / name
    return path.read_bytes() if path.exists() else b""


def minutes(value):
    return int(value) if value else DEFAULT_TEST_MINUTES


def settings(branch_ids=None):
    """[(pk, defaults, created_on, modified_on)] for the settings in scope.

    `branch_ids` is the set of branches that exist; a row for a station that was
    not imported is left out and reported rather than failing the run.
    """
    out = []
    for row in load():
        if branch_ids is not None and row["BranchID"] not in branch_ids:
            continue
        out.append((
            row["DeviceSettingID"],
            {
                "branch_id": row["BranchID"],
                "settings_code": text(row["SettingCode"]),
                "header_1": text(row["Header1"]),
                "header_2": text(row["Header2"]),
                "additional_header_1": text(row["Header3"]),
                "additional_header_2": text(row["Header4"]),
                "footer_1": text(row["Footer1"]),
                "footer_2_arabic": text(row["Footer2"]),
                "paper_feed": row["PaperFeed"] or 0,
                "print_type": PRINT_TYPES.get(row["PrintType"], PrintType.LANDSCAPE),
                "print_logo": bool(row["IsPrintEnable"]),
                "logo": logo_bytes(row) or None,
                "logo_name": text(row["LogoName"])[:120],
                "logo_changed_on": moment(row["LogoChangedOn"]),
                "receipt_copies": row["NoOfReceiptCopy"] or 1,
                # No legacy column: it arrived with the client's feedback on the
                # new screens, so every imported row starts with it off.
                "share_on_whatsapp": False,
                "order_no_prefix": text(row["OrderNoStartCharacter"]).upper(),
                "customer_test_minutes": minutes(row["TestTimeSlot"]),
                "cashier_test_minutes": minutes(row["CashierTestTimeSlot"]),
                "round_off_mode": ROUND_OFF_MODES.get(row["RoundOff"], RoundOffMode.UPWARD),
                "round_off_step": ROUND_OFF_STEPS.get(row["RoundOffLimit"], Decimal("0.25")),
                **approval(row),
            },
            moment(row["CreatedOn"]),
            moment(row["ModifiedOn"]),
        ))
    return out


def skipped(branch_ids):
    """Rows left behind, so a run says so rather than quietly dropping them."""
    return [
        f"settings {row['SettingCode']} (station {row['BranchID']} not imported)"
        for row in load()
        if row["BranchID"] not in branch_ids
    ]


def without_logo():
    return [row["SettingCode"] for row in load() if not row.get("LogoFile")]
