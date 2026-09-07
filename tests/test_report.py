"""Health-report tests.

This report is designed to leave the machine — it gets pasted into chats and
tickets. So the tests that matter are the ones proving a credential cannot
survive the trip, including a credential this code has never heard of.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from doser.report import REDACTED, as_text, as_xml, explain, scrub


class FakeSettings:
    lldap_admin_password = "sup3r-s3cret-lldap-pw"
    immich_api_key = "imm1ch_KEY_abcdef123456"
    vaultwarden_admin_token = "vw-admin-t0ken-xyz"
    nextcloud_admin_password = "nc-pass-9876"
    hass_token = "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJhYmMifQ.SflKxwRJSMeKKF2QT4fwpMe"
    vikunja_token = None
    mealie_token = None
    paperless_token = None
    enrolment_signing_key = "0123456789abcdef0123456789abcdef"
    database_url = "postgresql+asyncpg://doser:pgPassw0rd@doser-db:5432/doser"
    domain = "purrbrews.test"


S = FakeSettings()


@pytest.mark.parametrize(
    "text,secret",
    [
        ("lldap failed with password sup3r-s3cret-lldap-pw", "sup3r-s3cret-lldap-pw"),
        ('401 {"key":"imm1ch_KEY_abcdef123456"}', "imm1ch_KEY_abcdef123456"),
        ("admin token vw-admin-t0ken-xyz rejected", "vw-admin-t0ken-xyz"),
        (
            "connect failed: postgresql+asyncpg://doser:pgPassw0rd@doser-db:5432/doser",
            "pgPassw0rd",
        ),
        ("GET https://admin:hunter2@nc.example.invalid/status.php", "hunter2"),
        ("Authorization: Bearer ghp_QQQQwwwweeeerrrrttttyyyy123456", "ghp_QQQQwwwweeee"),
        ('{"password":"nc-pass-9876"}', "nc-pass-9876"),
    ],
)
def test_known_secrets_never_survive(text: str, secret: str):
    assert secret not in scrub(text, S)


def test_unknown_upstream_secret_also_caught():
    """The pattern layer must catch a secret the settings never knew about —
    an upstream quoting its own key back at us, for instance."""
    out = scrub("remote said: api_key=zzzzTOPSECRETzzzz9999 rejected", S)
    assert "zzzzTOPSECRETzzzz9999" not in out
    assert REDACTED in out


def test_jwt_shape_is_redacted_anywhere():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    assert jwt not in scrub(f"upstream returned {jwt} in the body", S)


def test_harmless_text_is_not_mangled():
    ok = "immich 200 OK in 43ms at https://immich.purrbrews.test"
    assert scrub(ok, S) == ok


def test_empty_and_none_are_safe():
    assert scrub(None, S) == ""
    assert scrub("", S) == ""


# ------------------------------------------------------------ explanations --
# "[Errno 11001] getaddrinfo failed" is accurate and useless. For a tool used
# three times a year, the error has to say what to do next.


def test_dns_failure_names_the_setting_to_change():
    out = explain("[Errno 11001] getaddrinfo failed", "https://nextcloud.purrbrews.test", S)
    assert "does not resolve" in out
    assert "DOSER_DOMAIN" in out
    assert "purrbrews.test" in out  # tells you the current value
    assert "[Errno 11001]" in out  # original preserved, not thrown away


def test_connection_refused_distinguished_from_dns():
    out = explain("[Errno 10061] target machine actively refused it", "http://10.0.0.5:2283", S)
    assert "resolved but nothing is listening" in out
    assert "10.0.0.5:2283" in out


def test_timeout_and_tls_and_auth_each_get_their_own_hint():
    assert "did not answer" in explain("ReadTimeout: timed out", "https://x.test", S)
    tls = explain("SSLCertVerificationError: verify failed", "https://x.test", S)
    assert "DOSER_VERIFY_TLS" in tls
    assert "credential was rejected" in explain("immich 401: unauthorized", "https://x.test", S)


def test_unrecognised_error_is_passed_through_unchanged():
    raw = "something nobody anticipated"
    assert explain(raw, "https://x.test", S) == raw


# --------------------------------------------------------------- rendering --
REPORT = {
    "generated_at": "2026-09-06T10:22:31+00:00",
    "environment": {"doser_version": "0.1.0", "python": "3.14.7"},
    "config": {"domain": "purrbrews.test", "hass_mode": "oidc_jit"},
    "summary": {"total": 2, "ok": 1, "unhealthy": 1, "not_configured": 0},
    "connectors": [
        {
            "name": "lldap", "label": "lldap", "tier": "source", "required": True,
            "configured": True, "base_url": "https://lldap.purrbrews.test",
            "ok": True, "status": "ok", "detail": "reachable", "latency_ms": 41,
        },
        {
            "name": "immich", "label": "Immich", "tier": "api", "required": True,
            "configured": True, "base_url": "https://immich.purrbrews.test",
            "ok": False, "status": "error",
            "detail": "ConnectError: getaddrinfo failed", "latency_ms": None,
        },
    ],
}


def test_text_report_contains_the_useful_bits():
    out = as_text(REPORT)
    assert "[ OK ] lldap" in out
    assert "[ERR ] immich" in out
    assert "getaddrinfo failed" in out
    assert "1 ok · 1 unhealthy" in out


def test_text_report_has_no_trailing_whitespace():
    """It gets pasted into chats and diffs; trailing spaces are noise."""
    assert not [ln for ln in as_text(REPORT).split("\n") if ln != ln.rstrip()]


def test_xml_report_is_well_formed():
    root = ET.fromstring(as_xml(REPORT))
    assert root.tag == "doserHealthReport"
    names = [c.get("name") for c in root.find("connectors")]
    assert names == ["lldap", "immich"]
    immich = root.find("connectors/connector[@name='immich']")
    assert immich.get("status") == "error"
    assert immich.find("detail").text == "ConnectError: getaddrinfo failed"


def test_xml_escapes_hostile_detail_text():
    """A connector error containing markup must not break the document."""
    report = {**REPORT, "connectors": [
        {**REPORT["connectors"][0], "detail": '</connector><evil>&"\'</evil>'}
    ]}
    root = ET.fromstring(as_xml(report))  # would raise if unescaped
    assert root.find("connectors/connector/detail").text == '</connector><evil>&"\'</evil>'
