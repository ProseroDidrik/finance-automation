"""Easy Auth integration för App Service.

App Service Easy Auth (Entra ID provider) sätter sin platform-proxy framför
containern. När en användare är inloggad skickar proxyn vidare ett base64-
kodat JSON-paket i headern ``X-MS-CLIENT-PRINCIPAL``. Vi:

  1. Avkodar paketet och plockar ut claims (oid, email, groups, ...).
  2. Kollar att användaren tillhör Maestro-gruppen i Entra ID.

Authentication (är användaren inloggad?) hanteras av Easy Auth-platformen
— icke-inloggade requests aldrig når containern. Authorization (har den
inloggade användaren Maestro-rollen?) är vårt jobb och görs här.

Konfiguration (Azure-sidan):
    az containerapp/webapp config -- konfigurera Auth provider = Microsoft
    + auto-redirect on unauth + token store enabled. Lägg till
    ``groups`` i id-token-claims under "Token configuration" på App
    Registration så Easy Auth inkluderar dem i headern.

Konfiguration (env):
    MAESTRO_GROUP_ID    — object-id (GUID) för Maestro-gruppen i Entra ID.
                          Krävs i prod; saknas → require_maestro 500:ar.
    DEV_AUTH_BYPASS     — sätt till "1" lokalt för att hoppa över auth.
                          Endast för dev — failar fast om satt i prod.
    DEV_AUTH_USER_EMAIL — fake-email att visa i loggar i bypass-mode.
                          Default "dev@local".

Header-format (referens):
    {
      "auth_typ": "aad",
      "claims": [
        {"typ": "preferred_username", "val": "did@prosero.se"},
        {"typ": "name",  "val": "Didrik Wachtmeister"},
        {"typ": "oid",   "val": "<user object id>"},
        {"typ": "groups","val": "<group object id>"},   # en rad per grupp
        ...
      ],
      "name_typ": "...",
      "role_typ": "..."
    }
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

# Standard claim-types Entra ID skickar via Easy Auth. ``groups`` kommer som
# en rad per grupp (claim-typen upprepas), inte en kommaseparerad sträng.
# Easy Auth V2 emittar kort-namnet "groups"; V1 och vissa token-config
# emittar URI-formen — vi accepterar båda för att slippa 403-baisser
# beroende på platform-version.
CLAIM_OID = "http://schemas.microsoft.com/identity/claims/objectidentifier"
CLAIM_GROUPS = "groups"
CLAIM_GROUPS_URI = "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
CLAIM_EMAIL = "preferred_username"
CLAIM_NAME = "name"


@dataclass(frozen=True)
class CurrentUser:
    oid: str | None
    email: str | None
    name: str | None
    groups: tuple[str, ...]

    @property
    def display(self) -> str:
        return self.email or self.name or self.oid or "(okänd)"


def _decode_principal(header_value: str | None) -> dict | None:
    """Avkoda base64-JSON från X-MS-CLIENT-PRINCIPAL. None om saknas/ogiltig."""
    if not header_value:
        return None
    try:
        raw = base64.b64decode(header_value)
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("Kunde inte avkoda X-MS-CLIENT-PRINCIPAL: %s", e)
        return None


def _claims_lookup(payload: dict) -> dict[str, list[str]]:
    """Returnera claim_type → list[values]. ``groups`` upprepas en rad/grupp."""
    out: dict[str, list[str]] = {}
    for c in payload.get("claims", []) or []:
        t = c.get("typ")
        v = c.get("val")
        if t is None or v is None:
            continue
        out.setdefault(t, []).append(v)
    return out


def _principal_from_payload(payload: dict) -> CurrentUser:
    claims = _claims_lookup(payload)

    def first(*keys: str) -> str | None:
        for k in keys:
            if k in claims and claims[k]:
                return claims[k][0]
        return None

    return CurrentUser(
        oid=first(CLAIM_OID, "oid"),
        email=first(CLAIM_EMAIL, "email", "upn"),
        name=first(CLAIM_NAME),
        groups=tuple(claims.get(CLAIM_GROUPS, []) + claims.get(CLAIM_GROUPS_URI, [])),
    )


def _dev_user() -> CurrentUser:
    """Fake-user för lokal dev när DEV_AUTH_BYPASS=1."""
    return CurrentUser(
        oid="00000000-0000-0000-0000-000000000000",
        email=os.environ.get("DEV_AUTH_USER_EMAIL", "dev@local"),
        name="Dev (DEV_AUTH_BYPASS)",
        groups=(),
    )


def _bypass_enabled() -> bool:
    """Endast utveckling. Vägrar slå på sig om vi körs på App Service
    (heuristik: WEBSITE_SITE_NAME är alltid satt där)."""
    if os.environ.get("DEV_AUTH_BYPASS") != "1":
        return False
    if os.environ.get("WEBSITE_SITE_NAME"):
        log.error("DEV_AUTH_BYPASS=1 ignoreras eftersom WEBSITE_SITE_NAME är satt (App Service).")
        return False
    return True


def current_user(request: Request) -> CurrentUser | None:
    """Returnera inloggad användare baserat på X-MS-CLIENT-PRINCIPAL.

    None → ingen header / felaktig header / Easy Auth har inte vidarebefodrat
    en authenticated user. I prod borde detta inte hända: Easy Auth omdirigerar
    vid behov innan requesten når oss.
    """
    if _bypass_enabled():
        return _dev_user()
    payload = _decode_principal(request.headers.get("x-ms-client-principal"))
    if payload is None:
        return None
    return _principal_from_payload(payload)


def is_maestro(user: CurrentUser, group_id: str | None = None) -> bool:
    """Kollar att user.groups innehåller MAESTRO_GROUP_ID."""
    gid = group_id or os.environ.get("MAESTRO_GROUP_ID")
    if not gid:
        # Fail closed: utan konfigurerad grupp finns ingen Maestro-policy
        # att tillämpa, och vi vill inte tyst släppa igenom alla.
        log.error("MAESTRO_GROUP_ID saknas — alla requests faller på authorization.")
        return False
    return gid in user.groups


# --- ASGI middleware: gate /api/* på Maestro-grupp -----------------------------

EXEMPT_PATHS = frozenset({"/api/health"})


def install_auth_middleware(app) -> None:
    """Registrera middleware som gatear alla /api/*-rutter på Maestro-grupp.

    /api/health är exempt så App Service liveness-probe kan slå mot den
    utan att gå via Easy Auth-proxyn. Allt utanför /api/ (StaticFiles
    frontend) släpps igenom — frontend bundle innehåller ingen sekretess
    och Easy Auth själv omdirigerar oinloggade besökare till login.
    """
    @app.middleware("http")
    async def _maestro_gate(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in EXEMPT_PATHS:
            return await call_next(request)

        # DEV_AUTH_BYPASS=1 → släpp igenom utan Maestro-check. Bypass är
        # tänkt för lokal dev där varken Easy Auth eller MAESTRO_GROUP_ID
        # är konfigurerat; vägrar slå på sig om WEBSITE_SITE_NAME är satt.
        if _bypass_enabled():
            request.state.user = _dev_user()
            return await call_next(request)

        user = current_user(request)
        if user is None:
            return JSONResponse(
                {"detail": "Inte inloggad — sessionen kan ha gått ut."},
                status_code=401,
            )
        if not is_maestro(user):
            log.info("Forbidden: %s saknar Maestro-grupp", user.display)
            return JSONResponse(
                {"detail": "Saknar behörighet (Maestro-grupp krävs)."},
                status_code=403,
            )
        request.state.user = user
        return await call_next(request)
