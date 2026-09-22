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
            ],
        }
    ],
}

SPECS = {
    "drawer_brand": BRAND,
    "drawer_category": CATEGORY,
    "drawer_vehicle_type": VEHICLE_TYPE,
}
