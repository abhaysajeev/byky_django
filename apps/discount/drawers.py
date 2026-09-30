"""Drawer specs for the Discount Card masters (theme/templates/byky/partials/drawer.html)."""

COMPANY = {
    "id": "company", "label": "Company", "kind": "select", "required": True,
    "options_from": "companies_list", "option_key": "name", "system_only": True,
}

CARD_TYPE = {
    "drawer_id": "drawerCardType",
    "scr_name": "card_type",
    "model": "discount.CardType",
    "add_label": "Add Card Type",
    "title_field": "name",
    "sections": [{
        "title": "",
        "fields": [
            COMPANY,
            {"id": "code", "label": "Card Type Code", "kind": "text", "required": True},
            {"id": "name", "label": "Card Type Name", "kind": "text", "required": True},
        ],
    }],
}

CARD_GRADE = {
    "drawer_id": "drawerCardGrade",
    "scr_name": "card_grade",
    "model": "discount.CardGrade",
    "add_label": "Add Card Grade",
    "title_field": "name",
    "sections": [{
        "title": "",
        "fields": [
            COMPANY,
            {"id": "card_type", "label": "Card Type", "kind": "select", "required": True,
             "options_from": "card_types_list", "option_key": "name", "width": 12},
            {"id": "code", "label": "Grade Code", "kind": "text", "required": True},
            {"id": "name", "label": "Grade Name", "kind": "text", "required": True},
        ],
    }],
}

SPECS = {
    "drawer_card_type": CARD_TYPE,
    "drawer_card_grade": CARD_GRADE,
}
