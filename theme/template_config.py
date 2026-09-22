"""Theme configuration, ported from the wireframe's config/template.py.

Light theme only, no customizer, and the menu is not Vuexy-fixed -- the Byky
sidebar pins itself through sidebar-integration.css and --byky-sb-w, and
menu_fixed=True would fight that rule.
"""

TEMPLATE_CONFIG = {
    "layout": "vertical",
    "theme": "light",
    "my_skins": "default",
    "has_semi_dark": False,
    "rtl_mode": False,
    "has_customizer": False,
    "display_customizer": False,
    "content_layout": "compact",
    "navbar_type": "fixed",
    "header_type": "fixed",
    "menu_fixed": False,
    "menu_collapsed": False,
    "footer_fixed": False,
    "show_dropdown_onhover": True,
    "customizer_controls": [],
}

THEME_VARIABLES = {
    "template_name": "Byky RMS",
    "template_suffix": "Enterprise Vehicle Rental Management",
    "template_version": "1.0.0",
}
