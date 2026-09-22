"""Maps TEMPLATE_CONFIG onto the class names the layout templates expect.

Ported from the wireframe's web_project/template_helpers/theme.py, minus
set_layout/import_class: this project has one layout and does not import it by
building a dotted path from a string.
"""

from django.conf import settings


class TemplateHelper:
    @staticmethod
    def init_context(context):
        config = settings.TEMPLATE_CONFIG
        context.update({
            "layout": config.get("layout"),
            "primary_color": config.get("primary_color"),
            "theme": config.get("theme"),
            "skins": config.get("my_skins"),
            "semiDark": config.get("has_semi_dark"),
            "rtl_mode": config.get("rtl_mode"),
            "has_customizer": config.get("has_customizer"),
            "display_customizer": config.get("display_customizer"),
            "content_layout": config.get("content_layout"),
            "navbar_type": config.get("navbar_type"),
            "header_type": config.get("header_type"),
            "menu_fixed": config.get("menu_fixed"),
            "menu_collapsed": config.get("menu_collapsed"),
            "footer_fixed": config.get("footer_fixed"),
            "show_dropdown_onhover": config.get("show_dropdown_onhover"),
            "customizer_controls": config.get("customizer_controls"),
        })
        return context

    @staticmethod
    def map_context(context):
        context["header_type_class"] = ""
        context["navbar_type_class"] = (
            "layout-navbar-fixed" if context.get("navbar_type") == "fixed"
            else "" if context.get("navbar_type") == "static"
            else "layout-navbar-hidden"
        )
        context["menu_collapsed_class"] = (
            "layout-menu-collapsed" if context.get("menu_collapsed") else ""
        )
        context["menu_fixed_class"] = (
            "layout-menu-fixed" if context.get("menu_fixed") else ""
        )
        context["footer_fixed_class"] = (
            "layout-footer-fixed" if context.get("footer_fixed") else ""
        )
        context["rtl_mode_value"], context["text_direction_value"] = (
            ("rtl", "rtl") if context.get("rtl_mode") else ("ltr", "ltr")
        )
        context["show_dropdown_onhover_value"] = (
            "true" if context.get("show_dropdown_onhover") else "false"
        )
        context["semi_dark_value"] = "true" if context.get("semiDark") else "false"
        context["display_customizer_class"] = (
            "" if context.get("display_customizer") else "customizer-hide"
        )
        if context.get("content_layout") == "wide":
            context["container_class"] = "container-fluid"
            context["content_layout_class"] = "layout-wide"
        else:
            context["container_class"] = "container-xxl"
            context["content_layout_class"] = "layout-compact"
        context["navbar_detached_class"] = (
            "navbar-detached" if context.get("navbar_detached") else ""
        )
        return context

    @staticmethod
    def get_theme_variables(scope):
        return settings.THEME_VARIABLES[scope]

    @staticmethod
    def get_theme_config(scope):
        return settings.TEMPLATE_CONFIG[scope]
