"""Settings for the BYKY rebuild.

One settings module, driven by environment variables. `.env` is read for local
development; in Docker the variables come from the compose file.
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


# --- Core -------------------------------------------------------------------

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,drone-scarcity-nectar.ngrok-free.dev")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Applications -----------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Registers the index-expression wrappers (OpClass) that the fare app's
    # exclusion constraints render; ArrayField alone never needed it.
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
]

# Project apps. One per module, added as each module is built
# (portal -> company -> crew -> fleet -> rental -> devices -> ...).
LOCAL_APPS = [
    "core",
    "theme",
    "apps.company",
    "apps.portal",
    "apps.devices",
    "apps.crew",
    "apps.fleet",
    "apps.fare",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# django.contrib.auth is deliberately absent: this project has its own
# permission model (see design/rbac.md). The custom user extends
# AbstractBaseUser only, which needs no auth app installed.

MIDDLEWARE = [
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "core.middleware.CurrentUserMiddleware",
    "core.middleware.CompanyTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
                "theme.context_processors.sidebar_menu",
                "theme.context_processors.alert_feed",
            ],
            "libraries": {"theme": "theme.templatetags.theme"},
            # The ported sidebar templates use these filters with no {% load %},
            # exactly as in the wireframe. Without this every page fails to render.
            "builtins": [
                "django.templatetags.static",
                "theme.templatetags.theme",
            ],
        },
    },
]

# Exposed as settings.TEMPLATE_CONFIG / settings.THEME_VARIABLES for the theme helpers.
from theme.template_config import TEMPLATE_CONFIG, THEME_VARIABLES  # noqa: E402,F401

# --- Database ---------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "byky"),
        "USER": os.environ.get("POSTGRES_USER", "byky"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "byky"),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

AUTH_USER_MODEL = "core.User"

# --- Sessions ---------------------------------------------------------------
# Web sign-in is a Django session; there is no idle timeout (design/03-login.md
# decision 3) and no "remember me" (decision 7), so the cookie dies with the
# browser.

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# Not simply `not DEBUG`: a deployment can have DEBUG=False before it has TLS
# in front of it (this project's first QA pass -- IP:port, no domain/cert
# yet). A Secure cookie over plain HTTP is silently dropped by the browser,
# not degraded -- that would look like login working, then every session
# instantly vanishing. Explicit env var, defaulting to the old `not DEBUG`
# behaviour so nothing changes for an already-TLS'd deployment that doesn't
# set it.
FORCE_HTTPS = env_bool("DJANGO_FORCE_HTTPS", not DEBUG)
SESSION_COOKIE_SECURE = FORCE_HTTPS
CSRF_COOKIE_SECURE = FORCE_HTTPS
SECURE_SSL_REDIRECT = FORCE_HTTPS
if FORCE_HTTPS:
    # This project sits behind a reverse proxy (whatever terminates TLS --
    # host nginx, Caddy, etc.) that forwards plain HTTP internally; without
    # this, Django can't tell the original request was HTTPS and the redirect
    # above loops forever.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Off by default even under TLS: HSTS is heavily cached by browsers and a
    # mistake here is hard to undo for real visitors. Turn on deliberately
    # once TLS is confirmed stable, not as part of this switch.
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))

# --- API --------------------------------------------------------------------

REST_FRAMEWORK = {
    # SimpleJWT's JWTAuthentication is not listed: its module imports
    # django.contrib.auth.models, which cannot load without that app installed
    # (see INSTALLED_APPS). How the apps' bearer tokens are checked is decided
    # with the login API (design/03-login.md section 6.2). Until then no API
    # takes a token, and the default stays IsAuthenticated -- closed, not open.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # Every API reply, errors included, in the {code, message, data} envelope.
    "EXCEPTION_HANDLER": "core.api.exception_handler",
    # drf-spectacular's introspecting AutoSchema, not DRF's own (which only
    # produces the old CoreAPI schema format). Every /api/v1/ view still
    # needs its own @extend_schema (core/schema.py) -- these views answer
    # with plain dicts, not serializers, so there is nothing to auto-detect
    # a response shape from without it.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # DRF's default is django.contrib.auth's AnonymousUser, and that app is not
    # installed -- importing it would fail on every call without a token.
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_THROTTLE_RATES": {
        "update_check": "60/minute",
        # A waiting tablet polls once a minute; these leave room for retries.
        "registration_ip": "30/minute",
        "registration_installation": "12/minute",
        # Below the 15-attempt/5-minute account lockout (apps/portal/auth.py),
        # so a real person mistyping a password is never the one who hits
        # this first -- it only blunts brute force spread across usernames.
        "app_login": "10/minute",
        # Refresh is meant to be called often and automatically; this only
        # stops a runaway or broken client, not real traffic.
        "app_refresh": "30/minute",
        # Called before login as often as after, so it needs its own room --
        # not shared with app_login/app_refresh, which imply a session exists.
        "server_time_ip": "60/minute",
        "server_time_installation": "30/minute",
    },
    # Proxies in front of Django. 0 = trust only the connection's own address,
    # so a client cannot pick its throttle bucket with X-Forwarded-For. Set it
    # to the number of proxies in production, or every tablet shares one bucket.
    "NUM_PROXIES": int(os.environ.get("DJANGO_NUM_PROXIES", "0")),
}

# --- API documentation (drf-spectacular) -------------------------------------
#
# Serves the OpenAPI schema and a Swagger UI page for every /api/v1/ endpoint,
# generated from the @extend_schema decorators on the views themselves
# (core/schema.py has the shared building blocks) -- so this project's own
# design/*.md handouts and the live Swagger page are built from the same
# examples, rather than the handout being retyped by hand each time the app
# team needs it.
SPECTACULAR_SETTINGS = {
    "TITLE": "BYKY App API",
    "DESCRIPTION": (
        "The contract for the operator, manager and employee apps. Every reply "
        "is {code, message, data} (see the 200/4xx examples per endpoint) -- "
        "the app switches on `code`, never on `message`. Design reasoning and "
        "the source proc each rule replaces live in design/ alongside the code, "
        "not here; this page is the request/response contract only."
    ),
    "VERSION": "1.0.0",
    # This project has no "list installed apps" schema use case (no
    # ModelViewSets, no browsable API forms) -- only the hand-annotated
    # /api/v1/ views matter, so nothing outside them is worth generating.
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    # Every {app} URL is one Django view serving 2-3 channels with different
    # response shapes (operator vs employee login, for one) -- component
    # names would otherwise collide across them.
    "COMPONENT_SPLIT_REQUEST": True,
    # Assets served locally via drf-spectacular-sidecar + WhiteNoise, not a
    # jsdelivr/unpkg CDN call every time someone opens the docs page.
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    # The raw schema at /api/schema/ is for tooling (codegen, Postman import),
    # not a page anyone opens by hand -- keep it out of its own UI listing.
    "SERVE_INCLUDE_SCHEMA": False,
    "SORT_OPERATIONS": False,
}

# Lifetimes from design/03-login.md section 6.2.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "UPDATE_LAST_LOGIN": False,
    "SIGNING_KEY": SECRET_KEY,
}

# --- Internationalisation ---------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Dubai")
USE_I18N = True
USE_TZ = True

# --- Static and media -------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_MAX_AGE = 31536000
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Map tiles need a referer; Django's default strips it (see CLAUDE.md, Maps).
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
