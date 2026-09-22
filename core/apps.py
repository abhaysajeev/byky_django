from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Core"

    def ready(self):
        # Registers AppJWTAuthenticationScheme with drf-spectacular's
        # extension registry (a metaclass side effect of importing the
        # module) -- has to run once before /api/schema/ is ever requested,
        # and app-ready is the one place guaranteed to run before that.
        import core.schema  # noqa: F401
