# Vendored from MyFITContainer e9931f0: app/garmin_client.py
# Source of truth stays MFC; changes here are marked "mcp-garmin addition".

"""Garmin Connect client (portiert aus MyActivitySync app/sync/garmin.py).

Replicates the garth 0.4.47 SSO flow used by cellTrainer:
  1) GET  /sso/embed                (gather cookies)
  2) GET  /sso/signin               (extract _csrf from HTML)
  3) POST /sso/signin               (form login)
  4) detect MFA -> POST /sso/verifyMFA/loginEnterMfaCode
  5) extract ticket from success HTML
  6) GET  /oauth-service/oauth/preauthorized   (OAuth1 consumer-only)
  7) POST /oauth-service/oauth/exchange/user/2.0 (OAuth1 -> OAuth2)
Upload: POST /upload-service/upload (multipart, Bearer).
Rename: PUT /activity-service/activity/{id}.
Get (MFC-Ergänzung): GET /activitylist-service/…/activities (Liste) und
GET /download-service/files/activity/{id} (ZIP mit Original-FIT).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

CONSUMER_KEY = "fc3e99d2-118c-44b8-8ae3-03370dde24c0"
CONSUMER_SECRET = "E08WAR897WEy2knn7aFBrvegVAf0AFdWBBF"
UA_DEFAULT = "GCM-iOS-5.7.2.1"
UA_OAUTH = "com.garmin.android.apps.connectmobile"

SSO_BASE = "https://sso.garmin.com"
CONNECT_API = "https://connectapi.garmin.com"

# Garmin-Aktivitäts-Sichtbarkeit (accessControlRuleDTO.typeKey). Die Werte sind
# Garmins eigene Schlüssel; das PUT braucht NUR den typeKey (kein typeId).
# private = Only me · subscribers = My connections · groups = connections + groups
# · public = Everyone.
GARMIN_PRIVACY_KEYS = ("private", "subscribers", "groups", "public")

# Garmin-Höhenquelle je Aktivität (metadataDTO.elevationCorrected): "device" =
# aufgezeichnete Gerätehöhe (Korrektur AUS), "dem" = digitale Höhenmodelldaten
# (Korrektur AN). device ist der Default.
GARMIN_ELEVATION_KEYS = ("device", "dem")


def _local_and_gmt(day: str, measured_at: Optional[datetime],
                   home_tz: Optional[str]) -> tuple[datetime, datetime]:
    """(lokale Wall-Clock, echter UTC-Instant) für Garmin-Zeitstempel.
    measured_at = naive lokale Messzeit (oder None -> Tag 12:00 lokal). home_tz =
    IANA-Name (Europe/Berlin); fehlt/ungültig -> lokal wird wie UTC behandelt
    (kein Offset). Garmin braucht BEIDE getrennt: Local zeigt die Wanduhrzeit,
    GMT den Instant — sie identisch zu setzen war der Bug (+Offset zu spät)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    local = measured_at or datetime.fromisoformat(f"{day}T12:00:00")
    local = local.replace(microsecond=0, tzinfo=None)
    tz = None
    if home_tz:
        try:
            tz = ZoneInfo(home_tz)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    if tz is None:
        return local, local
    gmt = local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return local, gmt


def _build_weight_fit(day: str, weight_kg: float, percent_fat=None,
                      muscle_mass=None, percent_hydration=None, bone_mass=None,
                      measured_at: Optional[datetime] = None,
                      home_tz: Optional[str] = None) -> bytes:
    """Minimaler Weight-Scale-FIT (FileType.WEIGHT) für den Garmin-Upload —
    Gewicht (kg) + optionale Körperzusammensetzung. Zeitstempel = echter
    UTC-Instant der lokalen Messzeit (measured_at) bzw. Tag 12:00 lokal."""
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.weight_scale_message import WeightScaleMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    _, gmt = _local_and_gmt(day, measured_at, home_tz)
    ts = int(gmt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    b = FitFileBuilder(auto_define=True)
    fid = FileIdMessage()
    fid.type = FileType.WEIGHT
    fid.manufacturer = Manufacturer.GARMIN.value
    fid.time_created = ts
    b.add(fid)
    ws = WeightScaleMessage()
    ws.timestamp = ts
    ws.weight = float(weight_kg)
    if percent_fat is not None:
        ws.percent_fat = float(percent_fat)
    if muscle_mass is not None:
        ws.muscle_mass = float(muscle_mass)
    if percent_hydration is not None:
        ws.percent_hydration = float(percent_hydration)
    if bone_mass is not None:
        ws.bone_mass = float(bone_mass)
    b.add(ws)
    return bytes(b.build().to_bytes())


class GarminError(Exception):
    pass


class GarminAuthError(GarminError):
    pass


class GarminUploadError(GarminError):
    pass


class GarminDuplicateError(GarminUploadError):
    pass


class LoginState(str, Enum):
    OK = "ok"
    NEEDS_MFA = "needs_mfa"


@dataclass
class OAuth1Token:
    oauth_token: str
    oauth_token_secret: str
    mfa_token: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str) -> "OAuth1Token":
        d = json.loads(s)
        return cls(**d)


@dataclass
class OAuth2Token:
    access_token: str
    refresh_token: str
    expires_in: int
    expires_at: float  # unix seconds

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str) -> "OAuth2Token":
        d = json.loads(s)
        return cls(**d)


def _percent_encode(s: str) -> str:
    # RFC 3986 unreserved: ALPHA / DIGIT / - . _ ~
    safe = "-._~"
    out: list[str] = []
    for ch in s.encode("utf-8"):
        c = chr(ch)
        if c.isalnum() or c in safe:
            out.append(c)
        else:
            out.append(f"%{ch:02X}")
    return "".join(out)


def _hmac_sha1_b64(key: str, data: str) -> str:
    mac = hmac.new(key.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def _build_oauth1_header(
    method: str,
    base_url: str,
    query: dict[str, str],
    body: dict[str, str],
    *,
    consumer_only: bool,
    oauth_token: Optional[str],
    token_secret: Optional[str],
) -> str:
    oauth_params: dict[str, str] = {
        "oauth_consumer_key": CONSUMER_KEY,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    if not consumer_only and oauth_token:
        oauth_params["oauth_token"] = oauth_token

    all_params: dict[str, str] = {}
    all_params.update(oauth_params)
    all_params.update(query)
    all_params.update(body)

    sorted_params = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}"
        for k, v in sorted(all_params.items())
    )
    sig_base = f"{method.upper()}&{_percent_encode(base_url)}&{_percent_encode(sorted_params)}"
    sig_key = f"{_percent_encode(CONSUMER_SECRET)}&{_percent_encode(token_secret or '')}"
    signature = _hmac_sha1_b64(sig_key, sig_base)
    oauth_params["oauth_signature"] = signature

    header = ", ".join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return f"OAuth {header}"


def _parse_query_string(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in s.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        try:
            from urllib.parse import unquote
            out[unquote(k)] = unquote(v)
        except Exception:
            out[k] = v
    return out


_CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')
_TICKET_RE = re.compile(r'embed\?ticket=([^"]+)"')
_TITLE_RE = re.compile(r"<title>([^<]+)</title>")


def _extract_csrf(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def _extract_ticket(html: str) -> Optional[str]:
    m = _TICKET_RE.search(html)
    return m.group(1) if m else None


def _is_mfa(html: str) -> bool:
    indicators = (
        "<title>MFA",
        "MFA Challenge",
        "mfa-code",
        "verifyMFA",
        "loginEnterMfaCode",
        "setupEnterMfaCode",
    )
    return any(ind in html for ind in indicators)


class GarminClient:
    """Stateful Garmin client. Tokens are exposed via properties for
    persistence in the application DB.

    Typical flow:
        client = GarminClient()
        state = await client.login(email, pw)
        if state == LoginState.NEEDS_MFA:
            state = await client.submit_mfa(code)
        # now client.oauth1_token / client.oauth2_token are set
        await client.upload(fit_bytes, "activity.fit")
    """

    def __init__(self) -> None:
        self.oauth1_token: Optional[OAuth1Token] = None
        self.oauth2_token: Optional[OAuth2Token] = None
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._mfa_csrf: str = ""
        self._mfa_signin_params: str = ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GarminClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---------- token persistence ----------

    def restore(self, oauth1_json: Optional[str], oauth2_json: Optional[str]) -> None:
        if oauth1_json:
            self.oauth1_token = OAuth1Token.from_json(oauth1_json)
        if oauth2_json:
            self.oauth2_token = OAuth2Token.from_json(oauth2_json)

    def export_tokens(self) -> tuple[str, str]:
        if not self.oauth1_token or not self.oauth2_token:
            raise GarminAuthError("no tokens to export")
        return self.oauth1_token.to_json(), self.oauth2_token.to_json()

    # ---------- login ----------

    async def login(self, email: str, password: str) -> LoginState:
        embed_params = "id=gauth-widget&embedWidget=true&gauthHost=" + SSO_BASE + "/sso"
        signin_params = (
            f"{embed_params}"
            f"&gauthHost={SSO_BASE}/sso/embed"
            f"&service={SSO_BASE}/sso/embed"
            f"&source={SSO_BASE}/sso/embed"
            f"&redirectAfterAccountLoginUrl={SSO_BASE}/sso/embed"
            f"&redirectAfterAccountCreationUrl={SSO_BASE}/sso/embed"
        )

        # Step 1: embed (gather cookies)
        embed_url = f"{SSO_BASE}/sso/embed?{embed_params}"
        await self._http.get(embed_url, headers={"User-Agent": UA_DEFAULT})

        # Step 2: signin GET (extract CSRF)
        signin_get_url = f"{SSO_BASE}/sso/signin?{signin_params}"
        r = await self._http.get(
            signin_get_url,
            headers={"User-Agent": UA_DEFAULT, "Referer": embed_url},
        )
        csrf = _extract_csrf(r.text)
        if not csrf:
            raise GarminAuthError("could not find CSRF token on signin page")

        # Step 3: signin POST
        signin_post_url = signin_get_url
        form = {
            "username": email,
            "password": password,
            "embed": "true",
            "_csrf": csrf,
        }
        r = await self._http.post(
            signin_post_url,
            headers={
                "User-Agent": UA_DEFAULT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": signin_get_url,
            },
            content="&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in form.items()),
        )
        html = r.text
        code = r.status_code

        if _is_mfa(html):
            self._mfa_csrf = _extract_csrf(html) or csrf
            self._mfa_signin_params = signin_params
            return LoginState.NEEDS_MFA

        if code == 429:
            raise GarminAuthError("rate limited (429) - wait a few minutes and retry")

        if "<title>Success" not in html:
            title_m = _TITLE_RE.search(html)
            title = title_m.group(1) if title_m else f"HTTP {code}"
            raise GarminAuthError(f"login failed: {title}")

        ticket = _extract_ticket(html)
        if not ticket:
            raise GarminAuthError("ticket not found in success response")

        await self._exchange_ticket(ticket)
        return LoginState.OK

    async def submit_mfa(self, code: str) -> LoginState:
        if not self._mfa_signin_params:
            raise GarminAuthError("submit_mfa called without prior login()")
        mfa_get_url = f"{SSO_BASE}/sso/verifyMFA/loginEnterMfaCode?{self._mfa_signin_params}"

        # Refresh CSRF from MFA page
        r = await self._http.get(mfa_get_url, headers={"User-Agent": UA_DEFAULT})
        fresh = _extract_csrf(r.text)
        csrf = fresh or self._mfa_csrf

        form = {
            "mfa-code": code,
            "embed": "true",
            "_csrf": csrf,
            "fromPage": "setupEnterMfaCode",
        }
        r = await self._http.post(
            mfa_get_url,
            headers={
                "User-Agent": UA_DEFAULT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": mfa_get_url,
            },
            content="&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in form.items()),
        )
        html = r.text
        if "<title>Success" not in html:
            title_m = _TITLE_RE.search(html)
            title = title_m.group(1) if title_m else f"HTTP {r.status_code}"
            raise GarminAuthError(f"MFA verification failed: {title}")
        ticket = _extract_ticket(html)
        if not ticket:
            raise GarminAuthError("ticket not found after MFA")
        await self._exchange_ticket(ticket)
        return LoginState.OK

    # ---------- OAuth exchanges ----------

    async def _exchange_ticket(self, ticket: str) -> None:
        base_url = f"{CONNECT_API}/oauth-service/oauth/preauthorized"
        query = {
            "ticket": ticket,
            "login-url": f"{SSO_BASE}/sso/embed",
            "accepts-mfa-tokens": "true",
        }
        header = _build_oauth1_header(
            "GET", base_url, query, {},
            consumer_only=True, oauth_token=None, token_secret=None,
        )
        qs = "&".join(
            f"{_percent_encode(k)}={_percent_encode(v)}"
            for k, v in sorted(query.items())
        )
        r = await self._http.get(
            f"{base_url}?{qs}",
            headers={"Authorization": header, "User-Agent": UA_OAUTH},
        )
        parts = _parse_query_string(r.text)
        if "oauth_token" not in parts or "oauth_token_secret" not in parts:
            raise GarminAuthError(f"preauthorized failed (HTTP {r.status_code}): {r.text[:200]}")
        self.oauth1_token = OAuth1Token(
            oauth_token=parts["oauth_token"],
            oauth_token_secret=parts["oauth_token_secret"],
            mfa_token=parts.get("mfa_token"),
        )
        await self._exchange_oauth1_for_oauth2()

    async def _exchange_oauth1_for_oauth2(self) -> None:
        if self.oauth1_token is None:
            raise GarminAuthError("no oauth1 token")
        url = f"{CONNECT_API}/oauth-service/oauth/exchange/user/2.0"
        body: dict[str, str] = {}
        if self.oauth1_token.mfa_token:
            body["mfa_token"] = self.oauth1_token.mfa_token
        header = _build_oauth1_header(
            "POST", url, {}, body,
            consumer_only=False,
            oauth_token=self.oauth1_token.oauth_token,
            token_secret=self.oauth1_token.oauth_token_secret,
        )
        body_str = "&".join(
            f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in body.items()
        )
        r = await self._http.post(
            url,
            headers={
                "Authorization": header,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": UA_OAUTH,
            },
            content=body_str,
        )
        if r.status_code != 200:
            raise GarminAuthError(f"oauth2 exchange HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        self.oauth2_token = OAuth2Token(
            access_token=j["access_token"],
            refresh_token=j["refresh_token"],
            expires_in=int(j["expires_in"]),
            expires_at=time.time() + float(j["expires_in"]),
        )

    async def _refresh_if_needed(self) -> None:
        if self.oauth2_token and not self.oauth2_token.is_expired:
            return
        if self.oauth1_token is None:
            raise GarminAuthError("no oauth1 token; re-login required")
        await self._exchange_oauth1_for_oauth2()

    # ---------- API: profile (test) ----------

    async def fetch_display_name(self) -> str:
        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile",
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "nk": "NT",
                "User-Agent": UA_DEFAULT,
            },
        )
        if r.status_code != 200:
            return ""
        j = r.json()
        return j.get("fullName") or j.get("displayName") or j.get("userName") or ""

    async def profile_display_id(self) -> str:
        """socialProfile.displayName (die GUID-artige Kennung, NICHT der volle
        Name) — wird von usersummary/wellness-Endpoints im Pfad verlangt. Gecacht."""
        if getattr(self, "_display_id", None):
            return self._display_id
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile", headers=await self._bearer())
        j = r.json() if r.status_code == 200 else {}
        self._display_id = j.get("displayName") or ""
        return self._display_id

    # ---------- API: Athleten-Profil (user-settings + HF-Zonen) ----------

    async def full_name(self) -> tuple[str, str]:
        """(Vorname, Nachname) aus socialProfile.fullName (best-effort-Split)."""
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile", headers=await self._bearer())
        full = ((r.json() or {}).get("fullName") if r.status_code == 200 else "") or ""
        parts = full.split()
        return (parts[0], " ".join(parts[1:])) if parts else ("", "")

    async def user_settings(self) -> dict:
        """userData aus /userprofile-service/userprofile/user-settings: Bio,
        VO₂max (Lauf/Rad), Laktatschwelle, Gewicht/Größe/Geburtsdatum/Geschlecht."""
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/userprofile/user-settings",
            headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"user-settings HTTP {r.status_code}")
        return ((r.json() or {}).get("userData") or {})

    async def personal_information(self) -> dict:
        """biometricProfile aus /userprofile-service/userprofile/personal-information:
        u. a. criticalSwimSpeed (Schwimm-Schwelle, mm/s), functionalThresholdPower,
        lactateThresholdHeartRate, vo2Max/vo2MaxCycling."""
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/userprofile/personal-information",
            headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"personal-information HTTP {r.status_code}")
        return ((r.json() or {}).get("biometricProfile") or {})

    async def heart_rate_zones(self) -> list[dict]:
        """HF-Zonen je Sport (/biometric-service/heartRateZones): je Eintrag
        zone1Floor..zone5Floor, maxHeartRateUsed, restingHeartRateUsed, sport."""
        r = await self._http.get(
            f"{CONNECT_API}/biometric-service/heartRateZones", headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"heartRateZones HTTP {r.status_code}")
        j = r.json()
        return j if isinstance(j, list) else []

    # ---------- API: gear (v2; Endpoints live mitgeschnitten 2026-06-18) ----------

    async def _bearer(self, content_type: bool = False) -> dict:
        await self._refresh_if_needed()
        assert self.oauth2_token
        h = {"Authorization": f"Bearer {self.oauth2_token.access_token}",
             "nk": "NT", "User-Agent": UA_DEFAULT}
        if content_type:
            h["Content-Type"] = "application/json"
        return h

    async def gear_profile_pk(self) -> int | None:
        """userProfilePk (für Gear-Create). Aus socialProfile, gecacht."""
        if getattr(self, "_gear_pk", None):
            return self._gear_pk
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile", headers=await self._bearer())
        j = r.json() if r.status_code == 200 else {}
        self._gear_pk = j.get("profileId") or j.get("id")
        return self._gear_pk

    async def list_gear(self, statuses: str = "ACTIVE") -> list[dict]:
        """Gear-Liste (v2): Items u. a. uuid, gearType (SHOES/BIKE), name, brand,
        status, distanceUsedMeters."""
        r = await self._http.get(
            f"{CONNECT_API}/gear-service/gear/v2/list",
            params={"start": 0, "limit": 100, "gearStatuses": statuses,
                    "sortOrder": "firstUseDate_desc"}, headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"list_gear HTTP {r.status_code}: {r.text[:160]}")
        data = r.json()
        return data if isinstance(data, list) else []

    async def create_gear(self, *, name: str, gear_type: str, brand: str = "",
                          first_use: str | None = None) -> str:
        """Legt Gear an (gear_type 'SHOES'|'BIKE'). Liefert die uuid."""
        body = {
            "userProfilePk": await self.gear_profile_pk(),
            "gearType": gear_type, "status": "ACTIVE", "name": name,
            "brand": brand or None, "usageType": "DISTANCE",
            "maxUsageDistanceMeters": 0, "maxUsageDurationSeconds": 0,
            "firstUseDate": first_use or datetime.utcnow().strftime("%Y-%m-%d"),
        }
        r = await self._http.post(
            f"{CONNECT_API}/gear-service/gear/v2",
            headers=await self._bearer(content_type=True), content=json.dumps(body))
        if r.status_code not in (200, 201):
            raise GarminError(f"create_gear HTTP {r.status_code}: {r.text[:160]}")
        return (r.json() or {}).get("uuid")

    async def rename_gear(self, gear_uuid: str, *, name: str, brand: str = "",
                          first_use: str | None = None) -> None:
        """Aktualisiert Name/Marke (Array-frei: PUT des Objekts) und optional das
        firstUseDate (Kaufdatum). Best-effort — der Edit-Endpoint ist nicht
        separat verifiziert."""
        items = await self.list_gear()
        obj = next((g for g in items if str(g.get("uuid")) == str(gear_uuid)), None)
        if obj is None:
            return
        obj = {**obj, "name": name, "brand": brand or None}
        if first_use:
            obj["firstUseDate"] = first_use
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/v2/{gear_uuid}",
            headers=await self._bearer(content_type=True), content=json.dumps(obj))
        if r.status_code not in (200, 204):
            raise GarminError(f"rename_gear HTTP {r.status_code}: {r.text[:160]}")

    async def retire_gear(self, gear_uuid: str) -> None:
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/v2/{gear_uuid}/status/RETIRED",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"retire_gear HTTP {r.status_code}: {r.text[:160]}")

    async def delete_gear(self, gear_uuid: str) -> None:
        r = await self._http.delete(
            f"{CONNECT_API}/gear-service/gear/v2/{gear_uuid}", headers=await self._bearer())
        if r.status_code not in (200, 204, 404):
            raise GarminError(f"delete_gear HTTP {r.status_code}: {r.text[:160]}")

    async def link_gear(self, gear_uuid: str, activity_id: int) -> None:
        """Weist die Ausrüstung einer Aktivität zu (PUT link/{uuid}/activity/{id})."""
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/link/{gear_uuid}/activity/{activity_id}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"link_gear HTTP {r.status_code}: {r.text[:160]}")

    async def unlink_gear(self, gear_uuid: str, activity_id: int) -> None:
        """Löst die Gear-Zuweisung einer Aktivität (PUT unlink/{uuid}/activity/{id})."""
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/unlink/{gear_uuid}/activity/{activity_id}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"unlink_gear HTTP {r.status_code}: {r.text[:160]}")

    async def multisport_children(self, activity_id: int) -> list[dict]:
        """Kind-Aktivitäten einer Multisport-Eltern-Aktivität: [{id, type}] mit
        typeKey (z. B. 'cycling', 'running', 'transition_v2'). Leer bei
        Nicht-Multisport oder Fehlern (best-effort)."""
        h = await self._bearer()
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}", headers=h)
        if r.status_code != 200:
            return []
        md = (r.json() or {}).get("metadataDTO") or {}
        out = []
        for cid in (md.get("childIds") or []):
            rc = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{cid}", headers=h)
            if rc.status_code != 200:
                continue
            tk = ((rc.json() or {}).get("activityTypeDTO") or {}).get("typeKey")
            out.append({"id": int(cid), "type": tk or ""})
        return out

    # ---------- API: health (read; Pull-Quelle, Endpoints aus python-garminconnect) ----------
    # Hinweis: aus der Bibliothek übernommen, live nachzuprüfen. Alle best-effort:
    # leere Liste/None bei Nicht-200, damit ein Endpoint-Problem den Pull nicht bricht.

    async def get_body_composition(self, start: str, end: str) -> list[dict]:
        """Gewicht/Körperzusammensetzung je Tag (YYYY-MM-DD..YYYY-MM-DD).
        Items: calendarDate, weight (Gramm!), bodyFat (%), muscleMass (Gramm)."""
        r = await self._http.get(
            f"{CONNECT_API}/weight-service/weight/dateRange",
            params={"startDate": start, "endDate": end}, headers=await self._bearer())
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("dateWeightList") or []

    async def get_blood_pressure(self, start: str, end: str) -> dict:
        """Blutdruck-Messungen im Bereich (rohes JSON mit measurementSummaries)."""
        r = await self._http.get(
            f"{CONNECT_API}/bloodpressure-service/bloodpressure/range/{start}/{end}",
            params={"includeAll": "true"}, headers=await self._bearer())
        return r.json() if r.status_code == 200 else {}

    async def get_resting_hr(self, display_name: str, day: str) -> float | None:
        """Ruhepuls eines Tages (usersummary)."""
        r = await self._http.get(
            f"{CONNECT_API}/usersummary-service/usersummary/daily/{display_name}",
            params={"calendarDate": day}, headers=await self._bearer())
        if r.status_code != 200:
            return None
        v = (r.json() or {}).get("restingHeartRate")
        return float(v) if v is not None else None

    async def get_hrv(self, day: str) -> float | None:
        """HRV eines Tages (Overnight-RMSSD, lastNightAvg) — Garmins HRV-Status
        ist RMSSD, NICHT SDNN."""
        r = await self._http.get(
            f"{CONNECT_API}/hrv-service/hrv/{day}", headers=await self._bearer())
        if r.status_code != 200:
            return None
        s = (r.json() or {}).get("hrvSummary") or {}
        v = s.get("lastNightAvg") or s.get("weeklyAvg")
        return float(v) if v is not None else None

    async def get_sleep(self, display_name: str, day: str) -> dict:
        """Schlaf-DTO eines Tages (Aufwach-Datum). Liefert das rohe dailySleepDTO
        (Sekunden je Phase: sleepTimeSeconds/deep/light/rem/awakeSleepSeconds,
        averageRespirationValue, Start/Ende). {} bei Nicht-200 — ein Endpoint-
        Problem bricht den Pull nicht."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/dailySleepData/{display_name}",
            params={"date": day, "nonSleepBufferMinutes": 60},
            headers=await self._bearer())
        if r.status_code != 200:
            return {}
        return (r.json() or {}).get("dailySleepDTO") or {}

    async def get_sleep_full(self, display_name: str, day: str) -> dict:
        """Wie get_sleep, aber der KOMPLETTE dailySleepData-Response (inkl.
        dailySleepDTO.sleepScores = Garmin Sleep Score). {} bei Nicht-200."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/dailySleepData/{display_name}",
            params={"date": day, "nonSleepBufferMinutes": 60},
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def get_hrv_summary(self, day: str) -> dict:
        """hrvSummary eines Tages: lastNightAvg (RMSSD), status (BALANCED/
        UNBALANCED/LOW/…), baseline. {} bei Nicht-200/fehlend."""
        r = await self._http.get(
            f"{CONNECT_API}/hrv-service/hrv/{day}", headers=await self._bearer())
        if r.status_code != 200:
            return {}
        return (r.json() or {}).get("hrvSummary") or {}

    async def get_daily_summary(self, display_name: str, day: str) -> dict:
        """Der volle usersummary-daily-Response eines Tages: restingHeartRate,
        Stress (averageStressLevel), Body Battery (bodyBatteryMostRecentValue,
        …Charged/…Drained), Steps, Kalorien, Atemfrequenz. {} bei Nicht-200."""
        r = await self._http.get(
            f"{CONNECT_API}/usersummary-service/usersummary/daily/{display_name}",
            params={"calendarDate": day}, headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def get_training_readiness(self, day: str) -> list[dict]:
        """Training-Readiness-Einträge eines Tages (metrics-service). Der
        MORGEN-Eintrag (Garmins Morning Report) hat inputContext =
        'AFTER_WAKEUP_RESET' — sein Fehlen heißt: Uhr seit dem Aufwachen noch
        nicht gesynct. [] bei Nicht-200."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/trainingreadiness/{day}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    async def get_training_status(self, day: str) -> dict:
        """Garmins aggregierter Training Status (PRODUCTIVE/MAINTAINING/…,
        Load Focus, Acute Load, VO2max). {} bei Nicht-200."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/trainingstatus/aggregated/{day}",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def delete_weight(self, day: str, sample_pk) -> None:
        """Löscht einen Gewichts-/Body-Composition-Eintrag (samplePk + Datum).
        DELETE /weight-service/weight/{day}/byversion/{samplePk}."""
        r = await self._http.delete(
            f"{CONNECT_API}/weight-service/weight/{day}/byversion/{sample_pk}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"delete_weight HTTP {r.status_code}: {r.text[:160]}")

    async def delete_blood_pressure(self, day: str, version) -> None:
        """Löscht eine Blutdruck-Messung (Datum + `version` aus get_blood_pressure).
        DELETE /bloodpressure-service/bloodpressure/{day}/{version}."""
        r = await self._http.delete(
            f"{CONNECT_API}/bloodpressure-service/bloodpressure/{day}/{version}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"delete_blood_pressure HTTP {r.status_code}: {r.text[:160]}")

    # ---------- API: upload ----------

    async def upload(
        self,
        fit_bytes: bytes,
        filename: str,
        start_utc: Optional[datetime] = None,
    ) -> Optional[int]:
        """Upload a FIT and resolve the resulting activity id.

        start_utc is the activity's start time in UTC (from intervals.icu). It is
        used to correlate the asynchronously-processed upload back to its
        Garmin activity id by matching startTimeGMT. Required for reliable
        post-upload resolution; without it we fall back to "newest activity",
        which is unreliable under back-to-back uploads.
        """
        await self._refresh_if_needed()
        assert self.oauth2_token
        url = f"{CONNECT_API}/upload-service/upload"
        files = {"file": (filename, fit_bytes, "application/octet-stream")}
        r = await self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_OAUTH,
            },
            files=files,
        )
        if r.status_code == 409 or "duplicate" in r.text.lower():
            raise GarminDuplicateError(f"duplicate (HTTP {r.status_code})")
        if not (200 <= r.status_code < 300):
            raise GarminUploadError(f"upload HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except Exception:
            data = {}
        log.info("garmin upload response: %s", json.dumps(data)[:500])
        result = (data or {}).get("detailedImportResult") or {}
        successes = result.get("successes") or []
        failures = result.get("failures") or []
        if not successes and failures:
            messages = json.dumps(failures)[:300]
            if "duplicate" in messages.lower():
                raise GarminDuplicateError(messages)
            raise GarminUploadError(messages)
        if successes:
            internal_id = successes[0].get("internalId")
            if internal_id is not None:
                return int(internal_id)
        if start_utc is None:
            log.warning("garmin upload: no start_utc provided; cannot resolve activity id")
            return None
        return await self._resolve_activity_by_start_time(start_utc)

    async def add_blood_pressure(
        self, day: str, systolic: int, diastolic: int,
        pulse: int = 0, notes: str = "",
        measured_at: Optional[datetime] = None, home_tz: Optional[str] = None,
    ) -> None:
        """Health-Hub: Blutdruckmessung eines Tages eintragen. Zeitstempel =
        lokale Messzeit (measured_at) bzw. Tag 12:00 lokal; Local = Wanduhrzeit,
        GMT = derselbe Instant in UTC (getrennt!). Garmin legt einen NEUEN
        Eintrag an (kein Update) — Aufrufer sorgt für Einmaligkeit je Tag."""
        await self._refresh_if_needed()
        assert self.oauth2_token
        local, gmt = _local_and_gmt(day, measured_at, home_tz)
        fmt = "%Y-%m-%dT%H:%M:%S.00"
        body = {
            "measurementTimestampLocal": local.strftime(fmt),
            "measurementTimestampGMT": gmt.strftime(fmt),
            "systolic": int(systolic), "diastolic": int(diastolic),
            "sourceType": "MANUAL", "notes": notes or "",
        }
        # Puls nur mitsenden, wenn plausibel (1..300) — Garmin lehnt pulse=0 mit
        # HTTP 400 ab; fehlt der Puls (kein RHR am Tag), akzeptiert es null.
        if pulse and 1 <= int(pulse) <= 300:
            body["pulse"] = int(pulse)
        r = await self._http.post(
            f"{CONNECT_API}/bloodpressure-service/bloodpressure",
            headers={"Authorization": f"Bearer {self.oauth2_token.access_token}",
                     "User-Agent": UA_DEFAULT, "Content-Type": "application/json"},
            json=body)
        if not (200 <= r.status_code < 300):
            raise GarminUploadError(
                f"blood pressure HTTP {r.status_code}: {r.text[:200]}")

    async def add_body_composition(
        self, day: str, weight_kg: float, percent_fat: Optional[float] = None,
        muscle_mass: Optional[float] = None, percent_hydration: Optional[float] = None,
        bone_mass: Optional[float] = None,
        measured_at: Optional[datetime] = None, home_tz: Optional[str] = None,
    ) -> None:
        """Health-Hub: Gewicht + Körperzusammensetzung eines Tages eintragen.
        Garmin nimmt das nur als Weight-Scale-FIT (kein JSON-Feld für Körperfett)
        über den Upload-Service entgegen. Gewicht in kg; Zeitstempel = lokale
        Messzeit (measured_at) bzw. Tag 12:00 lokal, als echter UTC-Instant.
        Aufrufer sorgt für Einmaligkeit je Tag (Upload = neuer Eintrag)."""
        fit_bytes = _build_weight_fit(day, weight_kg, percent_fat, muscle_mass,
                                      percent_hydration, bone_mass,
                                      measured_at=measured_at, home_tz=home_tz)
        await self._refresh_if_needed()
        assert self.oauth2_token
        files = {"file": (f"weight_{day}.fit", fit_bytes, "application/octet-stream")}
        r = await self._http.post(
            f"{CONNECT_API}/upload-service/upload",
            headers={"Authorization": f"Bearer {self.oauth2_token.access_token}",
                     "User-Agent": UA_OAUTH},
            files=files)
        # 409/„duplicate" = identische Messung schon vorhanden -> als ok werten.
        if r.status_code == 409 or "duplicate" in r.text.lower():
            return
        if not (200 <= r.status_code < 300):
            raise GarminUploadError(
                f"body composition HTTP {r.status_code}: {r.text[:200]}")

    async def find_activity_by_start_time(
        self,
        start_utc: datetime,
        tolerance_s: int = 60,
    ) -> Optional[int]:
        """Find an existing Garmin activity whose startTimeGMT matches the
        given UTC datetime within tolerance_s seconds. Returns the activityId
        with the highest value (most recent upload) when multiple match, so
        post-upload resolution prefers our just-created copy over older ones
        with the same start time.
        """
        await self._refresh_if_needed()
        assert self.oauth2_token
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)
        else:
            start_utc = start_utc.astimezone(timezone.utc)
        day = start_utc.strftime("%Y-%m-%d")
        r = await self._http.get(
            f"{CONNECT_API}/activitylist-service/activities/search/activities",
            params={"limit": 50, "start": 0, "startDate": day, "endDate": day},
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code != 200:
            return None
        arr = r.json() or []
        best_id: Optional[int] = None
        for a in arr:
            ts = a.get("startTimeGMT")
            if not ts:
                continue
            try:
                # Garmin returns "YYYY-MM-DD HH:MM:SS" in GMT (no tz suffix).
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if abs((dt - start_utc).total_seconds()) <= tolerance_s:
                aid = a.get("activityId")
                if aid is None:
                    continue
                if best_id is None or int(aid) > best_id:
                    best_id = int(aid)
        return best_id

    async def _resolve_activity_by_start_time(
        self,
        start_utc: datetime,
        attempts: int = 12,
        interval_s: float = 5.0,
    ) -> Optional[int]:
        for i in range(attempts):
            await asyncio.sleep(interval_s)
            aid = await self.find_activity_by_start_time(start_utc)
            if aid is not None:
                log.info("garmin resolved activity %s for start=%s (attempt %d)", aid, start_utc.isoformat(), i + 1)
                return aid
        log.warning("garmin: could not resolve activity id for start=%s after %d attempts", start_utc.isoformat(), attempts)
        return None

    async def set_activity_metadata(
        self,
        activity_id: int,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        retries: int = 3,
    ) -> bool:
        """Update activity name and/or description on Garmin. Idempotent:
        one GET to compare, then a single PUT that carries only the
        diverging fields. Pass `None` to leave a field untouched, `""`
        to clear it. Returns True iff a write was made.
        """
        if name is None and description is None:
            return False
        return await self._update_activity_field(
            activity_id, "activityName", name, retries=retries
        )

    async def rename_activity(
        self, activity_id: int, name: str, retries: int = 3
    ) -> bool:
        """Thin wrapper around _update_activity_field for name-only updates,
        kept so the orchestrator's metadata-resync path can address name and
        description independently with clearer log lines.
        """
        return await self._update_activity_field(
            activity_id, "activityName", (name or "").strip(), retries=retries
        )

    async def set_activity_description(
        self, activity_id: int, description: str, retries: int = 3
    ) -> bool:
        """Set a Garmin activity's description. Idempotent: GETs current
        description first and skips the PUT if it already matches. An
        empty string clears the description. Returns True iff a write was
        made.
        """
        description = description or ""
        return await self._update_activity_field(
            activity_id, "description", description, retries=retries
        )

    async def get_elevation_correction_state(
        self, activity_id: int
    ) -> Optional[bool]:
        """Return the current value of metadataDTO.elevationCorrected for
        an activity, or None if the GET fails / the field is absent.

        Used as a pre-check before calling disable_elevation_corrections,
        because the toggle endpoint is NOT idempotent — sending it when
        corrections are already off would re-enable them.
        """
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return None
        assert self.oauth2_token
        try:
            r = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "garmin elevation pre-GET %s failed: %s", activity_id, e
            )
            return None
        if r.status_code != 200:
            log.warning(
                "garmin elevation pre-GET %s HTTP %s", activity_id, r.status_code
            )
            return None
        try:
            md = (r.json() or {}).get("metadataDTO") or {}
        except Exception:  # noqa: BLE001
            return None
        v = md.get("elevationCorrected")
        return v if isinstance(v, bool) else None

    async def set_elevation_correction(
        self, activity_id: int, corrected: bool, retries: int = 3
    ) -> bool:
        """Set Garmin Connect's "Elevation Corrections" for one activity to a
        desired state: corrected=True -> use the digital elevation model (DEM),
        corrected=False -> use the recorded device elevation.

        Safe to call repeatedly — does a GET first and only sends the
        (non-idempotent) toggle when the current state differs from the target.

        Returns True if the activity is in the desired state after the call
        (either it already was, or we just toggled it). Returns False on:
          - GET failed AND we can't safely guess the current state (we refuse
            to send a blind toggle that could flip it the wrong way),
          - or the toggle POST returned a non-2xx after retries.

        Garmin's web UI uses POST /activity-service/activity/
        toggleElevationCorrection/{id} with a form-encoded body of
        `elevationCorrected=<current_state>` — the server inverts internally.
        Sending the CURRENT state therefore lands on the opposite (= target,
        since we only get here when current != target).
        """
        state = await self.get_elevation_correction_state(activity_id)
        if state == corrected:
            # Already in the desired state. No-op — success for the caller.
            return True
        if state is None:
            # Pre-check failed (network, auth, missing field — or Garmin hasn't
            # computed elevationCorrected yet right after upload). Refuse a blind
            # toggle; the caller retries on the next sync.
            log.warning(
                "garmin set_elevation_correction %s -> %s: pre-check inconclusive, "
                "skipping toggle", activity_id, corrected,
            )
            return False
        # state != corrected → send the toggle (body = current state, server flips).
        assert self.oauth2_token
        url = (
            f"{CONNECT_API}/activity-service/activity/"
            f"toggleElevationCorrection/{activity_id}"
        )
        body = f"elevationCorrected={'true' if state else 'false'}"
        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            assert self.oauth2_token
            r = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
                content=body,
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        log.warning(
            "garmin toggleElevationCorrection %s failed: HTTP %s body=%s",
            activity_id,
            r.status_code,
            r.text[:200],
        )
        return False

    async def disable_elevation_corrections(
        self, activity_id: int, retries: int = 3
    ) -> bool:
        """Ensure "Elevation Corrections" is OFF (use device elevation)."""
        return await self.set_elevation_correction(
            activity_id, False, retries=retries)

    async def _update_activity_field(
        self, activity_id: int, field: str, value: str, retries: int = 3
    ) -> bool:
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token

        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                current = (g.json() or {}).get(field) or ""
                if current == value:
                    return False
        except Exception as e:  # noqa: BLE001
            log.warning(
                "garmin update %s: pre-GET failed for %s: %s", field, activity_id, e
            )

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            assert self.oauth2_token
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id, field: value}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    # ---------- API: Get (MFC-Sync-Roundtrip) ----------

    async def list_recent_activities(self, limit: int = 20) -> list[dict]:
        """Die letzten Aktivitäten (neueste zuerst), je Eintrag u. a.
        activityId, activityName, startTimeGMT ("YYYY-MM-DD HH:MM:SS")."""
        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.get(
            f"{CONNECT_API}/activitylist-service/activities/search/activities",
            params={"limit": limit, "start": 0},
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code != 200:
            raise GarminError(f"list activities HTTP {r.status_code}: {r.text[:200]}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def download_original_fit(self, activity_id: int) -> Optional[bytes]:
        """Lädt die Original-Datei einer Aktivität (download-service liefert
        ein ZIP) und extrahiert die FIT. None, wenn das Original keine FIT
        ist (z. B. GPX-Import) — der Aufrufer überspringt dann."""
        import io
        import zipfile

        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.get(
            f"{CONNECT_API}/download-service/files/activity/{activity_id}",
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code != 200:
            raise GarminError(
                f"download {activity_id} HTTP {r.status_code}: {r.text[:200]}")
        data = r.content
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if not names:
                    return None
                return zf.read(names[0])
        except zipfile.BadZipFile:
            # Manche Antworten sind direkt die Datei (kein ZIP).
            if data[:12].find(b".FIT") != -1:
                return data
            return None

    async def set_activity_type(self, activity_id: int, type_key: str,
                                retries: int = 3) -> bool:
        """Setzt die Garmin-Aktivitätsart (activityTypeDTO.typeKey, z. B.
        'lap_swimming'). Idempotent: GET-Vergleich, dann ein PUT. True, wenn
        geschrieben wurde."""
        if not type_key:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                cur = ((g.json() or {}).get("activityTypeDTO") or {}).get("typeKey")
                if cur == type_key:
                    return False
        except Exception as e:  # noqa: BLE001
            log.warning("garmin type pre-GET %s failed: %s", activity_id, e)

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id,
                                    "activityTypeDTO": {"typeKey": type_key}}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    async def activity_exists(self, activity_id: int) -> Optional[bool]:
        """True/False, ob die Aktivität noch existiert; None bei Auth-/Netz-/
        sonstigem Fehler (dann NICHT als gelöscht behandeln). Genutzt vor einem
        Metadaten-Update, um einen toten Link (auf Garmin gelöschte Aktivität)
        zu erkennen und stattdessen neu hochzuladen."""
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return None
        assert self.oauth2_token
        try:
            r = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
        except Exception:  # noqa: BLE001
            return None
        if r.status_code == 404:
            return False
        if r.status_code == 200:
            return True
        return None

    async def delete_activity(self, activity_id: int) -> bool:
        """Löscht eine Aktivität (DELETE /activity-service/activity/{id}). True
        bei Erfolg oder wenn sie schon weg ist (404). Für den Replace-Pfad:
        geänderte Korrektur-FIT -> alte Aktivität löschen + neu hochladen."""
        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.request(
            "DELETE",
            f"{CONNECT_API}/activity-service/activity/{activity_id}",
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code in (200, 202, 204, 404):
            return True
        raise GarminError(
            f"delete activity {activity_id} HTTP {r.status_code}: {r.text[:160]}")

    async def set_activity_summary(self, activity_id: int,
                                   distance_m: Optional[float] = None,
                                   duration_s: Optional[float] = None,
                                   retries: int = 3) -> bool:
        """Überschreibt Distanz (m) und/oder Dauer (s) einer Aktivität
        (summaryDTO — wie der Edit in Garmin Connect Web). Idempotent per
        GET-Vergleich; best-effort: nicht jede Aktivitätsart erlaubt den
        Override, dann antwortet Garmin non-2xx -> False."""
        if distance_m is None and duration_s is None:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        body: dict = {}
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            cur = (g.json() or {}).get("summaryDTO") or {} if g.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            cur = {}
        if distance_m is not None and round(cur.get("distance") or -1, 1) != round(distance_m, 1):
            body["distance"] = float(distance_m)
        if duration_s is not None and round(cur.get("duration") or -1, 1) != round(duration_s, 1):
            body["duration"] = float(duration_s)
        if not body:
            return False

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id,
                                    "summaryDTO": body}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        log.warning("garmin summary override %s rejected", activity_id)
        return False

    # Sidecar-feel (1 = Strong … 5 = Weak) -> Garmin directWorkoutFeel
    # (0–100 in 25er-Schritten, 100 = best/Strong).
    _FEEL_TO_GARMIN = {1: 100.0, 2: 75.0, 3: 50.0, 4: 25.0, 5: 0.0}

    async def set_self_evaluation(self, activity_id: int,
                                  feel: Optional[int] = None,
                                  rpe: Optional[int] = None,
                                  retries: int = 3) -> bool:
        """Selbsteinschätzung wie im Connect-Web-Edit: summaryDTO.directWorkoutFeel
        (0/25/50/75/100) + directWorkoutRpe (RPE × 10, 10–100). Feldnamen aus
        Community-Wissen — live gegen das echte Konto verifizieren. Idempotent
        per GET-Vergleich; best-effort wie set_activity_summary (non-2xx ->
        False, nie fatal)."""
        if feel is None and rpe is None:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            cur = (g.json() or {}).get("summaryDTO") or {} if g.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            cur = {}
        body: dict = {}
        if feel is not None and feel in self._FEEL_TO_GARMIN:
            want = self._FEEL_TO_GARMIN[feel]
            if cur.get("directWorkoutFeel") != want:
                body["directWorkoutFeel"] = want
        if rpe is not None and 1 <= rpe <= 10:
            want = float(rpe * 10)
            if cur.get("directWorkoutRpe") != want:
                body["directWorkoutRpe"] = want
        if not body:
            return False

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id,
                                    "summaryDTO": body}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        log.warning("garmin self-evaluation %s rejected", activity_id)
        return False

    async def set_event_type_race(self, activity_id: int,
                                  retries: int = 3) -> bool:
        """Markiert eine Aktivität als Wettkampf (eventTypeDTO typeKey 'race').
        Idempotent per GET-Vergleich; setzt nur, nimmt nie zurück."""
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                cur = ((g.json() or {}).get("eventTypeDTO") or {}).get("typeKey")
                if cur == "race":
                    return False
        except Exception:  # noqa: BLE001
            pass

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({
                    "activityId": activity_id,
                    "eventTypeDTO": {"typeId": 1, "typeKey": "race"},
                }),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    async def set_activity_privacy(self, activity_id: int, type_key: str,
                                   retries: int = 3) -> bool:
        """Setzt die Sichtbarkeit einer Aktivität (accessControlRuleDTO.typeKey:
        private | subscribers | groups | public). Ändert NUR die Einstellung —
        kein Re-Upload. Idempotent per GET-Vergleich. True bei Erfolg/bereits so."""
        if type_key not in GARMIN_PRIVACY_KEYS:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                cur = ((g.json() or {}).get("accessControlRuleDTO") or {}).get("typeKey")
                if cur == type_key:
                    return True
        except Exception:  # noqa: BLE001
            pass

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({
                    "activityId": activity_id,
                    "accessControlRuleDTO": {"typeKey": type_key},
                }),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    # ---------- mcp-garmin additions (not present in MyFITContainer) ----------

    async def search_activities(
        self,
        limit: int = 20,
        start: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        activity_type: Optional[str] = None,
    ) -> list[dict]:
        """Activity list with the filters the activitylist-service accepts.
        start_date/end_date are local calendar dates ("YYYY-MM-DD"),
        activity_type a Garmin typeKey ("running", "cycling", "lap_swimming").
        """
        params: dict[str, Any] = {"limit": limit, "start": start}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if activity_type:
            params["activityType"] = activity_type
        r = await self._http.get(
            f"{CONNECT_API}/activitylist-service/activities/search/activities",
            params=params, headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"list activities HTTP {r.status_code}: {r.text[:200]}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def get_activity_detail(self, activity_id: int) -> dict:
        """Garmin's own activity record (summaryDTO, activityTypeDTO, …).
        Raises GarminError on 404 so a wrong id is not silently an empty dict."""
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}",
            headers=await self._bearer())
        if r.status_code == 404:
            raise GarminError(f"activity {activity_id} not found")
        if r.status_code != 200:
            raise GarminError(f"activity {activity_id} HTTP {r.status_code}")
        return r.json() or {}

    async def list_adhoc_challenges(self) -> list[dict]:
        """Social challenges against friends, newest first.

        Two endpoints are needed. `historical` returns only FINISHED
        challenges; the one running this month exists exclusively under
        `active`. Asking only historical is why the current month looks like it
        does not exist - which is how this was found.

        `active` entries carry playerCount 0 and players []; the real numbers
        come from get_adhoc_challenge.
        """
        found: dict[str, dict] = {}
        for path in ("active", "historical"):
            r = await self._http.get(
                f"{CONNECT_API}/adhocchallenge-service/adHocChallenge/{path}",
                headers=await self._bearer())
            if r.status_code != 200:
                if path == "historical":       # the finished ones are the bulk
                    raise GarminError(f"adhoc challenges HTTP {r.status_code}")
                continue
            for item in (r.json() or []):
                uuid = item.get("uuid")
                if uuid and uuid not in found:
                    found[uuid] = item
        return sorted(found.values(), key=lambda i: i.get("startDate") or "",
                      reverse=True)

    async def get_adhoc_challenge(self, uuid: str) -> dict:
        """One social challenge including its leaderboard (`players`). The
        list endpoint returns players=[]; only this one fills it."""
        r = await self._http.get(
            f"{CONNECT_API}/adhocchallenge-service/adHocChallenge/{uuid}",
            headers=await self._bearer())
        if r.status_code == 404:
            raise GarminError(f"challenge {uuid} not found")
        if r.status_code != 200:
            raise GarminError(f"challenge {uuid} HTTP {r.status_code}")
        return r.json() or {}

    # ---------- fitness metrics ----------

    async def personal_records(self) -> list[dict]:
        """All-time personal records (typeId per record kind, value in seconds
        or metres depending on the kind)."""
        r = await self._http.get(
            f"{CONNECT_API}/personalrecord-service/personalrecord/prs/"
            f"{await self.profile_display_id()}", headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"personal records HTTP {r.status_code}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def race_predictions(self) -> dict:
        """Garmin's current 5k/10k/half/marathon predictions, in seconds."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/racepredictions/latest/"
            f"{await self.profile_display_id()}", headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def fitness_age(self, day: str) -> dict:
        r = await self._http.get(f"{CONNECT_API}/fitnessage-service/fitnessage/{day}",
                                 headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def endurance_score(self, day: str) -> dict:
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/endurancescore?calendarDate={day}",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def hill_score(self, day: str) -> dict:
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/hillscore?calendarDate={day}",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def lifetime_totals(self) -> dict:
        r = await self._http.get(
            f"{CONNECT_API}/userstats-service/statistics/"
            f"{await self.profile_display_id()}", headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    # ---------- ranges: one call instead of one call per day ----------

    async def steps_range(self, start: str, end: str) -> list[dict]:
        r = await self._http.get(
            f"{CONNECT_API}/usersummary-service/stats/steps/daily/{start}/{end}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def body_battery_range(self, start: str, end: str) -> list[dict]:
        """Per day charged/drained. The response also carries the full intraday
        curve; callers are expected to drop it."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/bodyBattery/reports/daily",
            params={"startDate": start, "endDate": end}, headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def vo2max_range(self, start: str, end: str) -> list[dict]:
        """maxmet history: one entry per day, `generic` = running, `cycling`
        separate, either may be null."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/maxmet/daily/{start}/{end}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    # ---------- plan ----------

    async def planned_workouts(self, limit: int = 20) -> list[dict]:
        """The structured workouts stored in the account."""
        r = await self._http.get(f"{CONNECT_API}/workout-service/workouts",
                                 params={"start": 1, "limit": limit},
                                 headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"workouts HTTP {r.status_code}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def calendar_month(self, year: int, month: int) -> list[dict]:
        """calendarItems of one month. month is 1-12 here; Garmin's own path
        counts months from 0, which this method hides."""
        r = await self._http.get(
            f"{CONNECT_API}/calendar-service/year/{year}/month/{month - 1}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("calendarItems") or []

    # ---------- per-activity context ----------

    async def activity_weather(self, activity_id: int) -> dict:
        """Weather at the time of the activity. Garmin answers in Fahrenheit
        and mph here regardless of account settings."""
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}/weather",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def activity_time_in_zones(self, activity_id: int, kind: str = "hr") -> list[dict]:
        """Time per heart rate or power zone. Empty for activities recorded
        without the matching sensor, which is not an error."""
        path = "hrTimeInZones" if kind == "hr" else "powerTimeInZones"
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}/{path}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []
