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

SPECS = {
    "drawer_brand": BRAND,
    "drawer_category": CATEGORY,
}
