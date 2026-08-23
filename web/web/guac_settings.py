import logging.config
import os
import sys
from pathlib import Path

from django.utils.log import DEFAULT_LOGGING

from lib.cuckoo.common.config import Config as _CapeConfig
from lib.cuckoo.core.database import init_database

CUCKOO_PATH = os.path.join(Path.cwd(), "..")
sys.path.append(CUCKOO_PATH)

from lib.cuckoo.common.config import Config

# Build paths inside the project like this: BASE_DIR / "subdir".
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!

# Unique secret key generator.
# Secret key will be placed in secret_key.py file.
try:
    from .secret_key import SECRET_KEY  # noqa: F401
except ImportError:
    SETTINGS_DIR = os.path.abspath(os.path.dirname(__file__))
    # Using the same generation schema of Django startproject.
    from django.utils.crypto import get_random_string

    key = get_random_string(50, "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)")

    # Write secret_key.py
    _ = Path(os.path.join(SETTINGS_DIR, "secret_key.py")).write_text(f'SECRET_KEY = "{key}"')

    # Reload key.
    from .secret_key import SECRET_KEY  # noqa: F401

# SECURITY WARNING: don"t run with debug turned on in production!
DEBUG = True

LOGGING_CONFIG = None

WEB_AUTHENTICATION = getattr(Config("web"), "web_auth", {}).get("enabled", False)

ALLOWED_HOSTS = [
    "*",
]

INSTALLED_APPS = [
    "channels",
    "guac",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sessions",
    "django_extensions",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "web.guac_urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Database settings.
#
# Comment "we don't need it" is upstream-stale: the guac-web ASGI app
# still needs siteauth — AuthMiddlewareStack in asgi.py reads
# django_session on every WebSocket connect to attach scope["user"],
# and guac/views.index (the iframe page) checks WEB_AUTHENTICATION
# (django.contrib.auth) before rendering.
#
# Path is absolute because guac-web runs with cwd=/opt/CAPEv2/web (set
# by gunicorn WorkingDirectory in the systemd unit), and a relative
# "siteauth.sqlite" would resolve to /opt/CAPEv2/web/siteauth.sqlite —
# an empty stub file shipped by cape-core that has no tables.  The
# real, migrated siteauth lives at /var/lib/cape/django/siteauth.sqlite
# (see settings.py for the matching path + the rationale: cape services
# must write to a location outside /opt/CAPEv2/ since dh-virtualenv
# leaves that tree read-only at runtime).
#
# Without this absolute path, every guac WebSocket connect crashes
# AuthMiddleware with `django.db.utils.OperationalError: no such table:
# django_session`, the asgi handler closes the WS with code 1000, and
# the browser-side Guacamole client gets a closing handshake before
# the first protocol frame.  Caught on sb deploy 2026-05-22.
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "/var/lib/cape/django/siteauth.sqlite"}}

ASGI_APPLICATION = "web.asgi.application"

# Internationalization
# https://docs.djangoproject.com/en/4.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.0/howto/static-files/

STATIC_URL = "/static/"

# Additional locations of static files
# STATICFILES_DIRS = [os.path.join(BASE_DIR, "static")]

STATIC_ROOT = os.path.join(BASE_DIR, "static")

STATICFILES_FINDERS = (
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
    #    "django.contrib.staticfiles.finders.DefaultStoragddeFinder",
)

LOG_LEVEL = "WARNING"
logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(levelname)s:%(name)s:%(message)s",
            },
            "django.server": DEFAULT_LOGGING["formatters"]["django.server"],
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": BASE_DIR / "guac-server.log",
                "formatter": "default",
                "maxBytes": 1024 * 1024 * 100,  # 100 mb
            },
            "gunicorn": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "default",
                "filename": BASE_DIR / "gunicorn.log",
                "maxBytes": 1024 * 1024 * 100,  # 100 mb
            },
            "django.server": DEFAULT_LOGGING["handlers"]["django.server"],
        },
        "loggers": {
            "": {
                "handlers": ["console"],
                "level": LOG_LEVEL,
                "propagate": True,
            },
            "django.utils.autoreload": {
                "handlers": ["console"],
                "level": "ERROR",
            },
            "django": {
                "handlers": ["file"],
                "level": LOG_LEVEL,
                "propagate": False,
            },
            "guac-session": {
                "handlers": ["file"],
                "level": LOG_LEVEL,
                "propagate": False,
            },
            "gunicorn.errors": {
                "level": LOG_LEVEL,
                "handlers": ["gunicorn"],
                "propagate": True,
            },
            "django.server": DEFAULT_LOGGING["loggers"]["django.server"],
        },
    }
)


_db = init_database(exists_ok=True)

# Create guac_sessions table if EITHER guac feature is on: the task-based guac
# (guacamole.enabled) AND the direct-VNC console (vnc_console_enabled) both use
# guac_sessions. Gating solely on vnc_console_enabled (default off) breaks task-based
# guac on a fresh deploy — its sessions can't be persisted (gemini review, PR #12).
_guac_cfg = _CapeConfig("web").guacamole
if _guac_cfg.get("enabled", False) or _guac_cfg.get("vnc_console_enabled", False):
    from lib.cuckoo.core.data.guac_session import GuacSession  # noqa: F401
    from lib.cuckoo.core.data.db_common import Base
    Base.metadata.create_all(_db.engine)
