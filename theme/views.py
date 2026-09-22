from django.views.generic import TemplateView

from theme.layout import TemplateLayout


class ThemedTemplateView(TemplateView):
    """Every screen extends this, so the shell context is always present."""

    def get_context_data(self, **kwargs):
        return TemplateLayout.init(self, super().get_context_data(**kwargs))
