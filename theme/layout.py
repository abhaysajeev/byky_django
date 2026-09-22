"""Puts the shell's context on a view.

The wireframe resolved its layout by importing
`templates.layout.bootstrap.<layout>` from a dotted path built at runtime. This
project has one layout, so the same values are a plain dict -- no import magic,
and nothing depends on the templates directory being an importable package.
"""

from theme.template_helpers import TemplateHelper

VERTICAL = {
    "is_menu": True,
    "is_navbar": True,
    "is_footer": True,
    "navbar_detached": True,
    "content_navbar": True,
}


class TemplateLayout:
    @staticmethod
    def init(view, context):
        context = TemplateHelper.init_context(context)
        context.update(VERTICAL)
        context["layout_path"] = "layout/layout_vertical.html"
        TemplateHelper.map_context(context)
        return context
