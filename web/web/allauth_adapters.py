import requests
import threading
import time

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.account.signals import email_confirmed, user_signed_up
from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.dispatch import receiver


# ── OIDC discovery-document cache ────────────────────────────────────────────
# allauth caches the discovery doc per adapter instance (i.e. per request), so
# every login initiation and every callback makes a blocking outbound HTTP call
# with no timeout. This process-level cache adds:
#   • a 1-hour TTL shared across all requests in the process
#   • 5 s connect / 10 s read timeouts so a slow IdP can't hang the server
#   • issuer validation per RFC 8414 §3
#   • double-checked locking to minimise redundant cold-start fetches

_OIDC_DISCOVERY_CACHE: dict = {}
_OIDC_DISCOVERY_LOCK = threading.Lock()
_OIDC_DISCOVERY_TTL = 3600  # seconds


def _get_cached_openid_config(server_url: str) -> dict:
    now = time.monotonic()
    with _OIDC_DISCOVERY_LOCK:
        entry = _OIDC_DISCOVERY_CACHE.get(server_url)
        if entry and (now - entry["ts"]) < _OIDC_DISCOVERY_TTL:
            return entry["doc"]

    resp = requests.get(server_url, timeout=(5, 10))
    resp.raise_for_status()
    doc = resp.json()

    # OIDC spec: issuer in the discovery doc MUST equal the URL it was fetched
    # from (minus the /.well-known/openid-configuration suffix).
    expected = server_url.replace("/.well-known/openid-configuration", "").rstrip("/")
    actual = (doc.get("issuer") or "").rstrip("/")
    if actual and actual != expected:
        raise ValueError(
            f"OIDC discovery issuer mismatch: expected {expected!r}, got {actual!r}"
        )

    with _OIDC_DISCOVERY_LOCK:
        existing = _OIDC_DISCOVERY_CACHE.get(server_url)
        if not existing or (time.monotonic() - existing["ts"]) >= _OIDC_DISCOVERY_TTL:
            _OIDC_DISCOVERY_CACHE[server_url] = {"doc": doc, "ts": time.monotonic()}
        else:
            doc = existing["doc"]

    return doc


try:
    from allauth.socialaccount.providers.openid_connect.views import (
        OpenIDConnectOAuth2Adapter as _BaseOIDCAdapter,
    )
    from allauth.socialaccount.providers.openid_connect.provider import (
        OpenIDConnectProvider as _BaseOIDCProvider,
    )

    class CachedOpenIDConnectOAuth2Adapter(_BaseOIDCAdapter):
        """Serves openid_config from the process-level cache instead of fetching
        it on every request."""

        @property
        def openid_config(self):
            if not hasattr(self, "_openid_config"):
                self._openid_config = _get_cached_openid_config(
                    self.get_provider().server_url
                )
            return self._openid_config

    class CachedOpenIDConnectProvider(_BaseOIDCProvider):
        """OpenID Connect provider using the cached adapter.

        Registered via SOCIALACCOUNT_PROVIDERS["openid_connect"]["provider_class"]
        in settings.py — the officially-supported allauth override path.
        """
        oauth2_adapter_class = CachedOpenIDConnectOAuth2Adapter

        @classmethod
        def get_package(cls):
            # allauth derives the URL module from get_package(); must point at
            # the real openid_connect package so its urls.py is picked up by
            # build_provider_urlpatterns(), not this module's package ("web").
            return "allauth.socialaccount.providers.openid_connect"

except ImportError:
    pass  # openid_connect provider not installed — no-op


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_groups(extra: dict) -> set:
    """Return the set of IdP group names from token extra data."""
    oidc_cfg = getattr(settings, "OIDC_CFG", None) or {}
    claim = oidc_cfg.get("groups_claim") or "groups"
    raw = extra.get(claim) or []
    if isinstance(raw, str):
        raw = [raw]
    return {g for g in raw if isinstance(g, str)}


def _group_set(config_key: str) -> set:
    """Parse a comma-separated group list from OIDC_CFG into a set."""
    oidc_cfg = getattr(settings, "OIDC_CFG", None) or {}
    return {
        g.strip()
        for g in (oidc_cfg.get(config_key) or "").split(",")
        if g.strip()
    }


# ── Account adapters ──────────────────────────────────────────────────────────

disposable_domain_list = []
if hasattr(settings, "DISPOSABLE_DOMAIN_LIST"):
    with open(settings.DISPOSABLE_DOMAIN_LIST, "r") as f:
        disposable_domain_list = [domain.strip() for domain in f]


class DisposableEmails(DefaultAccountAdapter):
    def clean_email(self, email):
        if email.rsplit("@", 1)[-1] in disposable_domain_list:
            raise forms.ValidationError("Admin banned disposable email services")
        return email

    def is_open_for_signup(self, request):
        return settings.REGISTRATION_ENABLED


if not settings.EMAIL_CONFIRMATION:

    @receiver(user_signed_up)
    def user_signed_up_(request, user, **kwargs):
        user.is_active = not settings.MANUAL_APPROVE
        user.save()


@receiver(email_confirmed)
def email_confirmed_(request, email_address, **kwargs):
    user = User.objects.get(email=email_address.email)
    user.is_active = not settings.MANUAL_APPROVE
    user.save()


class MySocialAccountAdapter(DefaultSocialAccountAdapter):

    def pre_social_login(self, request, sociallogin):
        """Reject IdP accounts whose email domain doesn't match the configured
        allowlist. Silently skipped when social_auth_email_domain is blank."""
        user_email = sociallogin.account.extra_data.get("email") or ""
        if user_email and settings.SOCIAL_AUTH_EMAIL_DOMAIN:
            domain = user_email.rsplit("@", 1)[-1]
            if domain != settings.SOCIAL_AUTH_EMAIL_DOMAIN:
                raise forms.ValidationError(
                    f"Please use an email with domain: {settings.SOCIAL_AUTH_EMAIL_DOMAIN}"
                )

    def is_open_for_signup(self, request, sociallogin):
        """Gate account provisioning on IdP group membership.

        When required_groups is blank, any IdP-authenticated user gets a CAPE
        account (appropriate when the Okta app assignment is the access gate).
        When set, only users in at least one listed group are provisioned —
        useful when the app is assigned broadly but CAPE access should be
        restricted to a subset.
        """
        required = _group_set("required_groups")
        if not required:
            return True
        return bool(_extract_groups(sociallogin.account.extra_data or {}) & required)

    def save_user(self, request, sociallogin, form=None):
        """Persist a new or returning SSO user.

        Runs on every login so IdP changes (email, group membership) propagate
        to Django on the user's next sign-in without a manual DB update.

        Username: derived from the email local-part. If that collides with an
        existing account (two identities sharing the same local-part across
        different domains), the first 8 chars of the IdP subject claim are
        appended to guarantee uniqueness.

        Role management: when admin_groups / superadmin_groups are configured,
        is_staff / is_superuser are reconciled against IdP group membership on
        every login. A user manually promoted in Django will be demoted if they
        are not in the corresponding IdP group. Leave both blank to opt out of
        IdP-driven role management entirely.
        """
        user = super().save_user(request, sociallogin, form)
        extra = sociallogin.account.extra_data or {}
        changed = False

        # ── email ──────────────────────────────────────────────────────────
        email = extra.get("email") or ""
        if email and user.email != email:
            user.email = email
            changed = True

        # ── username ───────────────────────────────────────────────────────
        identifier = (
            email
            or extra.get("preferred_username")
            or extra.get("sub")
            or user.username
            or ""
        )
        if identifier:
            base = identifier.split("@")[0] if "@" in identifier else identifier
            if User.objects.filter(username=base).exclude(pk=user.pk).exists():
                suffix = (extra.get("sub") or "")[:8]
                base = f"{base}_{suffix}" if suffix else base
            base = base[:150]
            if user.username != base:
                user.username = base
                changed = True

        # ── roles ──────────────────────────────────────────────────────────
        admin_groups = _group_set("admin_groups")
        super_groups = _group_set("superadmin_groups")
        if admin_groups or super_groups:
            user_groups = _extract_groups(extra)
            new_staff = bool(user_groups & (admin_groups | super_groups))
            new_super = bool(user_groups & super_groups)
            if user.is_staff != new_staff or user.is_superuser != new_super:
                user.is_staff = new_staff
                user.is_superuser = new_super
                changed = True

        if changed:
            user.save()

        return user
