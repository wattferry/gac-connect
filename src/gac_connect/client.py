"""The async client: sign-in, session refresh, reads and commands.

    async with aiohttp.ClientSession() as http:
        client = GacClient("AU", http)
        captcha = await client.start_captcha()
        # ... user solves the puzzle, giving x ...
        await client.request_sms(mobile, captcha, x)
        await client.login_sms(mobile, code)
        vehicles = await client.list_vehicles()
        status = await client.get_status(vehicles[0].vin)

Design notes:

* Requests are encrypted + signed here; responses are decrypted here. Nothing
  else in the library does I/O.
* The IoV access token lasts ~1 h and is refreshed on demand; the refresh token
  is single-use, so every rotation is persisted before the retry. A spent
  refresh raises :class:`AuthExpiredError` (reauth).
* Vehicle commands are never auto-retried (a duplicate can double-actuate); reads
  are refreshed-and-retried once on an invalid token.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from . import commands, vehicle
from .auth import Captcha
from .const import (
    CODE_COMMAND_ACCEPTED,
    ERR_REFRESH_SPENT,
    ERR_TOKEN_INVALID,
    HTTP_TIMEOUT,
    IOV_APP_ID,
    IOV_VERSION,
    MAIN_APP_ID,
    MIN_REQUEST_GAP,
    REGIONS,
    SSO_SERVICE_ID,
    USER_AGENT,
)
from .crypto import decrypt_envelope, encrypt_envelope, encrypt_sensitive
from .errors import (
    AuthExpiredError,
    CaptchaError,
    CommandError,
    LoginError,
    PinRequiredError,
    RateLimitedError,
    RegionError,
    TokenInvalidError,
)
from .keys import Material, load_material
from .models import Vehicle, VehicleStatus
from .session import MemoryStore, Session, TokenStore
from .signing import IOV_FIELDS, MAIN_FIELDS, sign


def _request_id() -> str:
    return f"{random.randint(0, 99_999_999):08d}"


def _now_ms() -> str:
    return str(int(time.time() * 1000))


class GacClient:
    def __init__(
        self,
        region: str,
        http: aiohttp.ClientSession,
        store: TokenStore | None = None,
        *,
        material: Material | None = None,
    ) -> None:
        if region not in REGIONS:
            raise RegionError(f"unknown region {region!r}; known: {', '.join(REGIONS)}")
        self.region = region
        self._cfg = REGIONS[region]
        self._http = http
        self._store = store or MemoryStore()
        self._m = material or load_material()
        self._session = Session(region=region)
        self._captcha: Captcha | None = None
        # Hard request throttle: no two gateway calls closer than MIN_REQUEST_GAP,
        # serialized, regardless of how the client is driven. Caps the absolute
        # request rate so a misbehaving caller (a looping automation, a spammed
        # button) can never hammer the gateway.
        self._req_lock = asyncio.Lock()
        self._last_request = 0.0

    # ---- lifecycle -------------------------------------------------------
    async def load(self) -> None:
        """Restore a persisted session (call once before use)."""
        self._session = await self._store.load()
        self._session.region = self.region

    async def _persist(self) -> None:
        await self._store.save(self._session)

    @property
    def session(self) -> Session:
        return self._session

    # ---- low-level transport --------------------------------------------
    async def _throttle(self) -> None:
        async with self._req_lock:
            gap = MIN_REQUEST_GAP - (time.monotonic() - self._last_request)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_request = time.monotonic()

    async def _post(self, host: str, path: str, wrapper: dict, headers: dict) -> Any:
        await self._throttle()
        url = f"https://{host}{path}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        async with self._http.post(url, data=_dumps(wrapper), headers=headers, timeout=timeout) as resp:
            raw = await resp.read()
            if resp.status == 429:
                retry = resp.headers.get("Retry-After")
                raise RateLimitedError(
                    "gateway rate-limited the request",
                    retry_after=float(retry) if retry and retry.isdigit() else None,
                )
        try:
            outer = _loads(raw)
        except ValueError as exc:
            raise CommandError(f"non-JSON response ({resp.status}) from {path}") from exc
        if isinstance(outer, dict) and outer.get("encryptData"):
            plain = decrypt_envelope(outer, self._response_key(host))
            if plain is not None:
                return plain
        return outer

    def _response_key(self, host: str):
        return (self._m.main if host == self._cfg["main"] else self._m.iov).private

    async def _main_call(self, path: str, body: Any, *, authorization: str | None = None) -> Any:
        wrapper, _, _ = encrypt_envelope(body, self._m.main.public)
        headers = {
            "user-agent": USER_AGENT, "fnc-app-type": "android", "locale": "en",
            "fnc-fnc-os-version-type": "2.0.25", "fnc-app-id": MAIN_APP_ID,
            "fnc-timestamp": _now_ms(), "region": self.region,
            "fnc-client-timezone": self._cfg["tz"], "fnc-requestId": _request_id(),
            "content-type": "application/json; charset=utf-8",
        }
        if authorization:
            headers["Authorization"] = authorization
        headers["sig"] = sign(headers, wrapper, fields=MAIN_FIELDS, hmac_key=self._m.main_hmac)
        return await self._post(self._cfg["main"], f"/gateway/v1{path}", wrapper, headers)

    async def _iov_call(self, path: str, body: Any, *, sensitive: dict[str, str] | None = None) -> Any:
        wrapper, key, iv = encrypt_envelope(body, self._m.iov.public)
        headers = {
            "user-agent": USER_AGENT, "fnc-app-type": "android", "locale": "en",
            "fnc-fnc-os-version-type": "2.0.25", "fnc-request-id": _request_id(),
            "content-type": "application/json; charset=utf-8", "fnc-version": IOV_VERSION,
            "fnc-app-id": IOV_APP_ID, "fnc-timestamp": _now_ms(),
            "fnc-icv-timezone": self._cfg["tz"],
        }
        if self._session.token:
            headers["token"] = self._session.token
        if sensitive:
            for name, value in sensitive.items():
                headers[name] = encrypt_sensitive(value, key, iv)
            headers["fnc-sensitive-fields"] = ",".join(sensitive)
        headers["sig"] = sign(headers, wrapper, fields=IOV_FIELDS, hmac_key=self._m.iov_hmac)
        return await self._post(self._cfg["iov"], f"/gateway{path}", wrapper, headers)

    @staticmethod
    def _data(resp: Any) -> Any:
        return resp.get("data") if isinstance(resp, dict) else None

    @staticmethod
    def _ok(resp: Any) -> bool:
        return isinstance(resp, dict) and bool(resp.get("success"))

    # ---- sign-in ---------------------------------------------------------
    async def start_captcha(self) -> Captcha:
        resp = await self._main_call("/support/api/captcha/v1/get/sec", {"captchaType": "blockPuzzle"})
        if not self._ok(resp):
            raise CaptchaError(f"could not start captcha: {_msg(resp)}")
        self._captcha = Captcha.from_data(resp["data"])
        return self._captcha

    async def _verify_captcha(self, x: int) -> Captcha:
        """Check the slide; on success the same Captcha carries the proofs."""
        if self._captcha is None:
            raise CaptchaError("call start_captcha first")
        c = self._captcha
        resp = await self._main_call("/support/api/captcha/v1/check/sec", {
            "captchaType": "blockPuzzle", "pointJson": c.point_json(x), "token": c.token,
        })
        if not self._ok(resp):
            raise CaptchaError("puzzle position rejected; fetch a new puzzle and retry")
        return c

    async def request_sms(self, mobile: str, x: int) -> None:
        """Verify the puzzle at offset ``x`` and send a login code to ``mobile``."""
        c = await self._verify_captcha(x)
        resp = await self._main_call(
            "/support/api/message/verify-code/sms/send-verify-code/sec",
            {"msgBizCode": "userLogin", "countryTelCode": self._cfg["tel"],
             "mobile": _local_mobile(mobile), "email": "", "captchaType": "blockPuzzle",
             "captchaTicket": c.ticket(x), "captchaRandomStr": c.point_json(x),
             "type": 1, "sendType": None},
        )
        if not self._ok(resp) or not self._data(resp).get("verifyCodeTicket"):
            raise LoginError(f"could not send SMS code: {_msg(resp)}")
        self._verify_ticket = self._data(resp)["verifyCodeTicket"]

    async def login_sms(self, mobile: str, code: str) -> None:
        """Complete sign-in with the SMS code, then mint an IoV session."""
        ticket = getattr(self, "_verify_ticket", None)
        if not ticket:
            raise LoginError("request_sms must succeed first")
        resp = await self._main_call("/iam/api/user/login/code/sec", {
            "verifyCodeTicket": ticket, "verifyCode": code, "type": 1,
            "countryTelCode": self._cfg["tel"], "mobile": _local_mobile(mobile), "email": "",
            "protocolList": ["COMMON_PRIVACY_POLICY", "COMMON_USER_AGREEMENT"],
        })
        if not self._ok(resp):
            raise LoginError(f"code login failed: {_msg(resp)}")
        d = self._data(resp)
        self._session.main_token = d.get("token")
        self._session.main_refresh_token = d.get("refreshToken")
        await self._establish_iov()

    async def _establish_iov(self) -> None:
        """Exchange the main token for an IoV ticket, then an IoV session."""
        resp = await self._main_call(
            "/iam/sso/login/sec", {"serviceId": SSO_SERVICE_ID},
            authorization=self._session.main_token,
        )
        ticket = self._data(resp)
        if isinstance(ticket, dict):
            ticket = ticket.get("value")
        if not self._ok(resp) or not isinstance(ticket, str):
            raise LoginError(f"SSO ticket exchange failed: {_msg(resp)}")
        sso = await self._iov_call("/sso/login", {"ticket": ticket})
        d = self._data(sso)
        if not (isinstance(d, dict) and d.get("token")):
            raise LoginError(f"IoV session exchange failed: {_msg(sso)}")
        self._apply_iov_tokens(d)
        await self._persist()

    def _apply_iov_tokens(self, d: dict) -> None:
        self._session.token = d.get("token")
        self._session.refresh_token = d.get("refreshToken")
        self._session.expire_time = d.get("expireTime")
        self._session.rexpire_time = d.get("rexpireTime")

    async def _refresh(self) -> None:
        if not self._session.refresh_valid:
            raise AuthExpiredError("refresh token expired; sign in again")
        resp = await self._iov_call("/refresh/token", {"refreshToken": self._session.refresh_token})
        d = self._data(resp)
        if isinstance(resp, dict) and resp.get("code") == ERR_REFRESH_SPENT:
            raise AuthExpiredError("refresh token spent; sign in again")
        if not (isinstance(d, dict) and d.get("token")):
            raise AuthExpiredError(f"refresh failed: {_msg(resp)}")
        self._apply_iov_tokens(d)
        await self._persist()  # persist the rotated pair BEFORE any retry

    # ---- reads -----------------------------------------------------------
    async def list_vehicles(self) -> list[Vehicle]:
        resp = await self._main_call("/icv/api/my-vehicle/list/sec", {},
                                     authorization=self._session.main_token)
        d = self._data(resp) or {}
        records = (d.get("bindVehicles") or []) + (d.get("authorizedVehicles") or [])
        return [Vehicle.from_record(r) for r in records if isinstance(r, dict)]

    async def get_status(self, vin: str) -> VehicleStatus:
        resp = await self._iov_read(commands.CATALOG["status"].path, {"vin": vin}, sensitive={"vin": vin})
        results = ((self._data(resp) or {}).get("results") or [{}])[0]
        return VehicleStatus.from_results(
            results,
            online=(self._data(resp) or {}).get("online"),
            updated_ms=(self._data(resp) or {}).get("updateTime"),
        )

    async def get_position(self, vin: str) -> dict:
        resp = await self._iov_read(commands.CATALOG["position"].path, {"vin": vin}, sensitive={"vin": vin})
        return self._data(resp) or {}

    async def _iov_read(self, path: str, body: Any, *, sensitive: dict | None = None) -> Any:
        """A read that refreshes-and-retries once on an invalid token."""
        if not self._session.access_valid and self._session.refresh_valid:
            await self._refresh()
        resp = await self._iov_call(path, body, sensitive=sensitive)
        if isinstance(resp, dict) and resp.get("code") == ERR_TOKEN_INVALID:
            await self._refresh()
            resp = await self._iov_call(path, body, sensitive=sensitive)
        return resp

    # ---- push channel ----------------------------------------------------
    async def mqtt_info(self) -> dict:
        """Broker details for the command-result feed (short-lived password)."""
        if not self._session.access_valid and self._session.refresh_valid:
            await self._refresh()
        resp = await self._iov_read("/mqtt/info", {})
        data = self._data(resp)
        if not isinstance(data, dict) or not data.get("host"):
            raise CommandError(f"push channel details unavailable: {_msg(resp)}")
        return data

    def decrypt_push(self, envelope: dict) -> Any:
        """Open a message from the push feed (same envelope as gateway responses)."""
        return decrypt_envelope(envelope, self._m.iov.private)

    # ---- commands --------------------------------------------------------
    async def command(self, vin: str, name: str, **overrides: Any) -> Any:
        cmd = commands.CATALOG.get(name)
        if cmd is None:
            raise CommandError(f"unknown command {name!r}")
        if cmd.pin:
            raise PinRequiredError(f"{name} needs the remote-control PIN, not yet supported")
        if not self._session.access_valid and self._session.refresh_valid:
            await self._refresh()
        body = commands.build_body(cmd, vin, overrides)
        resp = await self._iov_call(cmd.path, body, sensitive={"vin": vin})
        # Commands are NOT auto-retried on an invalid token (could double-actuate);
        # surface it so the caller decides.
        return _command_result(resp)

    async def climate_on(self, vin: str, *, temperature: float = 24.0, minutes: int = 30) -> Any:
        """Run the cabin A/C in auto mode at ``temperature`` °C for ``minutes``.

        Raises ``ValueError`` for a non-finite or out-of-range temperature (18–32 °C,
        rounded to 0.5) or duration (5–60 minutes) before anything is sent.
        """
        t, m = commands.validate_climate(temperature, minutes)
        return await self.command(vin, "aircon-on", temperature=t, currentDuration=m)

    async def climate_off(self, vin: str) -> Any:
        return await self.command(vin, "aircon-off")

    async def fridge_on(self, vin: str, *, mode: str = "refrigerate", temperature: float | None = None) -> Any:
        """Ask the car to run the fridge in ``mode`` ("refrigerate", "heat" or "freeze") at ``temperature`` °C.

        Without a temperature this library's default for the mode is sent
        (refrigerate 3, heat 42, freeze -12 °C), so omitting it while the fridge
        runs resets the target. Raises ``ValueError`` for an unknown mode or an
        out-of-range temperature before anything is sent. Sending this while the
        fridge runs changes its mode or temperature. An accepted request is not
        proof the car applied it; read the status to confirm.
        """
        code, t = commands.validate_fridge(mode, temperature)
        return await self.command(vin, "fridge-on", workingMode=code, refrigeratorTemperature=t)

    async def fridge_off(self, vin: str) -> Any:
        return await self.command(vin, "fridge-off")

    async def lock(self, vin: str) -> Any:
        return await self.command(vin, "lock")

    async def charge_now(self, vin: str, *, until_soc: bool = False) -> Any:
        return await self._reservation(vin, vehicle.charge_now_operation(until_soc=until_soc))

    async def charge_pause(self, vin: str) -> Any:
        return await self._reservation(vin, vehicle.pause_operation(ZoneInfo(self._cfg["tz"])))

    async def set_charge_window(self, vin: str, start: str, stop: str, *,
                                weekly: int = 0, until_soc: bool = False) -> Any:
        op = vehicle.window_operation(start, stop, ZoneInfo(self._cfg["tz"]),
                                      weekly=weekly, until_soc=until_soc)
        return await self._reservation(vin, op)

    async def _reservation(self, vin: str, operation: dict) -> Any:
        if not self._session.access_valid and self._session.refresh_valid:
            await self._refresh()
        resp = await self._iov_call(vehicle._RESERVATION, vehicle.reservation_body(vin, operation),
                                    sensitive={"vin": vin})
        return _command_result(resp)


def command_session_id(resp: Any) -> str | None:
    """The session id the service assigns to an accepted command, if it gives one."""
    from .push import normalize_id  # same normalisation as push results, so ids correlate

    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        return None
    ident = data.get("identifier")
    sid = data.get("sessionId") or (ident.get("sessionId") if isinstance(ident, dict) else None)
    return normalize_id(sid)


def _command_result(resp: Any) -> Any:
    """13001 "ongoing" is an accepted async command, not an error."""
    if isinstance(resp, dict):
        code = resp.get("code")
        if code == ERR_TOKEN_INVALID:
            raise TokenInvalidError("token invalid; refresh and reissue the command")
        if resp.get("success") or code in (CODE_COMMAND_ACCEPTED, str(CODE_COMMAND_ACCEPTED), "0000"):
            return resp
        raise CommandError(f"command rejected: {_msg(resp)}")
    return resp


def _msg(resp: Any) -> str:
    if isinstance(resp, dict):
        return str(resp.get("msg") or resp.get("code") or resp)[:200]
    return str(resp)[:200]


def _local_mobile(value: str) -> str:
    digits = "".join(c for c in value if c.isdigit())
    if digits.startswith("61"):
        digits = digits[2:]
    return digits.lstrip("0")


def _dumps(obj: Any) -> bytes:
    import json
    return json.dumps(obj, separators=(",", ":")).encode()


def _loads(raw: bytes) -> Any:
    import json
    return json.loads(raw)
