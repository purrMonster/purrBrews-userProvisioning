"""Configuration.

Service URLs follow the fleet's split-horizon convention: every app is reachable
at ``https://<subdomain>.${DOMAIN}`` both on the LAN (Pi-hole resolves straight
to the hosting node) and externally (via the Cloudflare Tunnel).  So a single
``DOSER_DOMAIN`` derives every base URL, and each one can still be overridden
individually with ``DOSER_<APP>_URL`` when a service lives somewhere unusual
(a raw LAN IP and port during bring-up, say).

Nothing here has a credential default.  A connector whose credential is missing
is *disabled and shown as disabled*, never silently no-op — see
docs/09-deployment.md §4.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOSER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- core -----------------------------------------------------------
    domain: str = "purrbrews.test"
    database_url: str = "sqlite+aiosqlite:///./doser.db"
    log_level: str = "INFO"

    # ---- auth -----------------------------------------------------------
    required_group: str = "purrbrews_infra_admins"
    trusted_proxy_cidrs: str = "127.0.0.1/32,::1/128"
    #: Local development only.  Bypasses forward-auth and pretends the caller is
    #: ``dev_operator``.  Refused outright when the app is not bound to loopback.
    dev_auth_bypass: bool = False
    dev_operator: str = "barista"

    # ---- engine ---------------------------------------------------------
    worker_concurrency: int = 4
    job_lease_seconds: int = 300
    job_max_attempts: int = 5
    reconcile_interval_seconds: int = 3600
    http_timeout_seconds: float = 15.0
    #: Verify TLS on outbound connector calls.  Set false only for a test fleet
    #: using self-signed certificates.
    verify_tls: bool = True

    # ---- node LAN IPs ---------------------------------------------------
    # Mirrors the *_LAN_IP variables in sieve's pihole-dns-bootstrap.sh.
    # Needed for the two services that have NO split-horizon DNS entry, so a
    # hostname simply will not resolve for them:
    #   * lldap    — on sieve, not routed through Traefik (known gap)
    #   * jellyfin — moved to roastery 2026-09-05, LAN-reachable directly and
    #                deliberately excluded from the bootstrap's HOST_TARGETS
    sieve_lan_ip: Optional[str] = None
    roastery_lan_ip: Optional[str] = None

    # ---- lldap (source of truth) ---------------------------------------
    lldap_url: Optional[str] = None
    lldap_port: int = 17170
    lldap_admin_user: Optional[str] = None
    lldap_admin_password: Optional[str] = None
    lldap_default_group: str = "purrbrews_users"

    # ---- user-facing targets -------------------------------------------
    immich_url: Optional[str] = None
    immich_api_key: Optional[str] = None
    immich_quota_gb: int = 50

    nextcloud_url: Optional[str] = None
    nextcloud_admin_user: Optional[str] = None
    nextcloud_admin_password: Optional[str] = None
    nextcloud_quota: str = "50 GB"
    #: "ldap" (accounts appear on first LDAP login) or "ocs" (pre-create via the
    #: provisioning API).  Never both — see docs/02 §5.
    nextcloud_mode: str = "ldap"

    vikunja_url: Optional[str] = None
    vikunja_token: Optional[str] = None

    actual_url: Optional[str] = None

    hass_url: Optional[str] = None
    hass_token: Optional[str] = None
    #: "oidc_jit"  — decision D5: HACS OIDC provider, account appears on first
    #:               SSO login.  doser holds no HA credentials.  DEFAULT.
    #: "local_ws"  — testing only: create the account through HA's *internal*
    #:               WebSocket API.  Undocumented, can break on any HA upgrade,
    #:               and requires doser to mint a password.  Opt in knowingly.
    hass_mode: str = "oidc_jit"

    jellyfin_url: Optional[str] = None
    jellyfin_api_key: Optional[str] = None

    mealie_url: Optional[str] = None
    mealie_token: Optional[str] = None

    paperless_url: Optional[str] = None
    paperless_token: Optional[str] = None

    vaultwarden_url: Optional[str] = None
    vaultwarden_admin_token: Optional[str] = None

    roundcube_url: Optional[str] = None
    #: Roundcube has no user store of its own — it authenticates against IMAP.
    #: Until a mail backend exists, provisioning is a documented no-op.
    mail_backend: Optional[str] = None

    # ---- notifications --------------------------------------------------
    ntfy_url: Optional[str] = None
    ntfy_topic: Optional[str] = None

    enrolment_signing_key: str = "dev-only-change-me"
    enrolment_ttl_hours: int = 72

    # ---- derived --------------------------------------------------------
    def service_url(self, attr: str, subdomain: str) -> str:
        """Explicit override if set, else ``https://<subdomain>.<domain>``.

        Subdomains here are the ones actually published by sieve's
        ``pihole-dns-bootstrap.sh`` (HOST_TARGETS), not guesses — notably
        ``homeassistant`` (not ``hass``), ``actualbudget`` (not ``actual``)
        and ``roundcube`` (not ``mail``).
        """
        override = getattr(self, attr, None)
        if override:
            return str(override).rstrip("/")
        return f"https://{subdomain}.{self.domain}"

    def lldap_base(self) -> str:
        """lldap has no split-horizon DNS entry — reach it by IP:port.

        It runs on sieve but is not behind Traefik (a known gap in the sieve
        stack), so ``lldap.${DOMAIN}`` does not resolve. Prefer an explicit
        DOSER_LLDAP_URL; otherwise derive it from DOSER_SIEVE_LAN_IP.
        """
        if self.lldap_url:
            return str(self.lldap_url).rstrip("/")
        if self.sieve_lan_ip:
            return f"http://{self.sieve_lan_ip}:{self.lldap_port}"
        return f"https://lldap.{self.domain}"  # will not resolve; error says so

    def jellyfin_base(self) -> str:
        """Jellyfin moved to roastery on 2026-09-05 and is deliberately not in
        the DNS bootstrap — it is reached directly on the LAN, not via Traefik."""
        if self.jellyfin_url:
            return str(self.jellyfin_url).rstrip("/")
        if self.roastery_lan_ip:
            return f"http://{self.roastery_lan_ip}:8096"
        return f"https://jellyfin.{self.domain}"  # will not resolve; error says so

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trusted_proxies(self) -> list[str]:
        return [c.strip() for c in self.trusted_proxy_cidrs.split(",") if c.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
