"""Drawer specs for the fleet module's screens."""

BRAND = {
    "drawer_id": "drawerBrand",
    "scr_name": "brand",
    "model": "fleet.Brand",
    "add_label": "Add Brand",
    "title_field": "brand_name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True,
                },
                {
                    "id": "brand_code",
                    "label": "Brand Code",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "brand_name",
                    "label": "Brand Name",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "description",
                    "label": "Description",
                    "kind": "textarea",
                    "required": False,
                },
                {
                    "id": "manufacturer",
                    "label": "Manufacturer",
                    "kind": "text",
                    "required": False,
                },
                {
                    "id": "website_link",
                    "label": "Website Link",
                    "kind": "text",
                    "required": False,
                    "placeholder": "https://",
                },
            ],
        }
    ],
}

CATEGORY = {
    "drawer_id": "drawerCategory",
    "scr_name": "category",
    "model": "fleet.Category",
    "add_label": "Add Category",
    "title_field": "category_name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True,
                },
                {
                    "id": "category_code",
                    "label": "Category Code",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "category_name",
                    "label": "Category Name",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "description",
                    "label": "Description",
                    "kind": "textarea",
                    "required": False,
                },
            ],
        }
    ],
}

VEHICLE_TYPE = {
    "drawer_id": "drawerVehicleType",
    "scr_name": "vehicle_type",
    "model": "fleet.VehicleType",
    "add_label": "Add Vehicle Type",
    "title_field": "vehicle_type_name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True,
                },
                {
                    "id": "category",
                    "label": "Category",
                    "kind": "select",
                    "required": True,
                    "options_from": "categories_list",
                    "option_key": "category_name",
                },
                {
                    "id": "brand",
                    "label": "Brand",
                    "kind": "select",
                    "required": True,
                    "options_from": "brands_list",
                    "option_key": "brand_name",
                },
                {
                    "id": "vehicle_type_code",
                    "label": "Vehicle Type Code",
                    "kind": "text",
                    "required": False,
                },
                {
                    "id": "vehicle_type_name",
                    "label": "Vehicle Type Name",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "description",
                    "label": "Description",
                    "kind": "textarea",
                    "required": False,
                },
                {
                    "id": "tax_percentage",
                    "label": "Tax Percentage",
                    "kind": "number",
                    "required": False,
                },
                {
                    "id": "other_tax",
                    "label": "Other Tax",
                    "kind": "number",
                    "required": False,
                },
            ],
        }
    ],
}

UOM = {
    "drawer_id": "drawerUom",
    "scr_name": "uom",
    "model": "fleet.UOM",
    "add_label": "Add UOM",
    "title_field": "uom_name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True,
                },
                {
                    "id": "uom_code",
                    "label": "UOM Code",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "uom_name",
                    "label": "UOM Name",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "description",
                    "label": "Description",
                    "kind": "textarea",
                    "required": False,
                },
            ],
        }
    ],
}

ASSET_TYPE = {
    "drawer_id": "drawerAssetType",
    "scr_name": "asset_type",
    "model": "fleet.AssetType",
    "add_label": "Add Asset Type",
    "title_field": "asset_type_name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True,
                },
                {
                    "id": "asset_type_code",
                    "label": "Asset Type Code",
                    "kind": "text",
                    "required": False,
                },
                {
                    "id": "asset_type_name",
                    "label": "Asset Type",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "description",
                    "label": "Description",
                    "kind": "textarea",
                    "required": False,
                },
            ],
        }
    ],
}

ASSET = {
    "drawer_id": "drawerAsset",
    "scr_name": "asset",
    "model": "fleet.Asset",
    "add_label": "Add Asset",
    "title_field": "asset_code",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True,
                },
                {
                    "id": "asset_code",
                    "label": "Asset Code",
                    "kind": "text",
                    "required": True,
                },
                {
                    "id": "asset_type",
                    "label": "Asset Type",
                    "kind": "select",
                    "required": True,
                    "options_from": "asset_types_list",
                    "option_key": "asset_type_name",
                },
                {
                    "id": "brand",
                    "label": "Brand",
                    "kind": "select",
                    "required": False,
                    "options_from": "brands_list",
                    "option_key": "brand_name",
                },
                {
                    "id": "serial_no",
                    "label": "Serial No",
                    "kind": "text",
                    "required": False,
                },
                {
                    "id": "manufacturer",
                    "label": "Manufacturer",
                    "kind": "text",
                    "required": False,
                },
                {
                    "id": "custodian",
                    "label": "Custodian",
                    "kind": "select",
                    "required": False,
                    "options_from": "employees_list",
                    "option_key": "name",
                },
                {
                    "id": "supplier",
                    "label": "Supplier",
                    "kind": "text",
                    "required": False,
                },
                {
                    "id": "purchase_invoice_no",
                    "label": "Purchase Invoice No",
                    "kind": "text",
                    "required": False,
                },
                {
                    "id": "description",
                    "label": "Description",
                    "kind": "textarea",
                    "required": False,
                },
                {
                    "id": "warranty_from_date",
                    "label": "Warranty From Date",
                    "kind": "date",
                    "required": False,
                },
                {
                    "id": "warranty_to_date",
                    "label": "Warranty To Date",
                    "kind": "date",
                    "required": False,
                },
                {
                    "id": "branch",
                    "label": "Branch",
                    "kind": "select",
                    "required": False,
                    "options_from": "branches_list",
                    "option_key": "name",
                },
            ],
        }
    ],
}

SPECS = {
    "drawer_brand": BRAND,
    "drawer_category": CATEGORY,
    "drawer_vehicle_type": VEHICLE_TYPE,
    "drawer_uom": UOM,
    "drawer_asset_type": ASSET_TYPE,
    "drawer_asset": ASSET,
}
