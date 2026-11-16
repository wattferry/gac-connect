"""The client version the gateway gates on: sent in the clear, and overridable.

An AU code send advertising an out-of-date version is refused by GAC with
1004002015 ("please try again later") while a byte-identical request advertising the
current version is accepted and delivered the code. These tests pin the two properties
that make that survivable: the value is never signed (so it can be changed without
invalidating a request), and a caller who knows their app's version can state it.
"""
import asyncio
import json
import time
from unittest.mock import Mock

from gac_connect.client import GacClient
from gac_connect.const import APP_VERSION
from gac_connect.limits import Limiter
from gac_connect.signing import MAIN_FIELDS, sign
from test_offline import _FakeHttp, _FakeResp  # the shared stand-in for the network

# start_captcha builds a Captcha from the reply, so the stand-in puzzle carries a
# well-formed secretKey (32B key @DS@ 16B iv), as the other captcha tests do.
PUZZLE = {"code": "0000", "success": True, "data": {
    "captchaType": "blockPuzzle", "secretKey": "K" * 32 + "@DS@" + "I" * 16, "token": "tok",
    "originalImageBase64": "", "jigsawImageBase64": "", "wordList": None}}
CHECK = {"code": "0000", "success": True, "data": {"result": True}}
STATUS = {"success": True, "data": {"results": [{}], "online": "1"}}
# The refusal GAC gives a code send that advertises a version it considers old.
SEND_REFUSED = {"code": "1004002015", "success": False, "data": None,
                "msg": "Verification code sending failed. Please try again later. (1001)",
                "extension": {"exName": "BusinessRuntimeException", "exSrcApp": "bff-aia-support",
                              "tracerId": "0" * 32}}


def _route(*, send=None):
    """Answer each endpoint the way GAC does, so a flow runs to the step under test."""
    def answer(url):
        body = PUZZLE
        if "send-verify-code" in url:
            body = send if send is not None else {"code": "0000", "success": True,
                                                  "data": {"verifyCodeTicket": "sms-ticket"}}
        elif "captcha/v1/check/sec" in url:
            body = CHECK
        elif "get_vehicle_status" in url:
            body = STATUS
        return _FakeResp(body=json.dumps(body).encode())
    return answer


def _limiter():
    return Limiter(min_gap=0.0)


def _signed_in(http, **kw):
    """A client holding both live token pairs, so calls reach the gateway directly."""
    client = GacClient("AU", http, limiter=_limiter(), **kw)
    now = int(time.time() * 1000)
    client._session.token, client._session.expire_time = "t", now + 3_600_000
    client._session.refresh_token, client._session.rexpire_time = "r", now + 3_600_000
    return client


def _versions(http):
    """The version every request would have advertised on the wire."""
    return [kw["headers"]["fnc-fnc-os-version-type"] for kw in http.kwargs]


def test_default_version_is_the_advertised_default():
    http = _FakeHttp(route=_route())
    asyncio.run(GacClient("AU", http, limiter=_limiter()).start_captcha())
    assert _versions(http) == [APP_VERSION]


def test_the_stated_version_reaches_both_gateways():
    """Sign-in rides the main account API and a status read rides the IoV gateway.

    The version is set in both call sites, so a caller who states their app's version
    fixes the refused code send and the vehicle calls, not just one of them.
    """
    http = _FakeHttp(route=_route())
    client = _signed_in(http, app_version="9.9.9")

    async def go():
        await client.get_status("V123")          # IoV gateway
        await client.start_captcha()             # main account API

    asyncio.run(go())
    assert http.calls == 2
    assert _versions(http) == ["9.9.9", "9.9.9"]


def test_the_advertised_version_is_outside_what_the_signature_covers():
    """Two requests differing only in advertised version sign identically.

    This is what makes an out-of-date default recoverable: the gateway reads the
    version in the clear, so changing it needs no re-signing and cannot invalidate a
    request. If the version ever joined signing.MAIN_FIELDS this test fails, which is
    the point: at that point an override would silently break every call.
    """
    http = Mock()
    client = GacClient("AU", http, limiter=_limiter())
    base = {"fnc-app-id": "1", "fnc-requestId": "12345678", "fnc-timestamp": "1794960000000",
            "fnc-fnc-os-version-type": APP_VERSION}
    moved = dict(base, **{"fnc-fnc-os-version-type": "9.9.9"})
    body = {"mobile": "0412345678", "countryTelCode": "+61"}

    assert "fnc-fnc-os-version-type" not in MAIN_FIELDS
    assert sign(base, body, fields=MAIN_FIELDS, hmac_key=client._m.main_hmac) == \
        sign(moved, body, fields=MAIN_FIELDS, hmac_key=client._m.main_hmac)


def test_a_refused_send_reports_as_a_login_failure_not_a_puzzle_one():
    """The refusal GAC gives for an old client reaches the caller as a refused send.

    The reply text ("please try again later") names the messaging service while the
    real cause is a version gate, so what the caller can rely on is the failure being
    a LoginError from the code send, distinct from a puzzle failure.
    """
    import pytest
    from gac_connect.errors import CaptchaError, LoginError

    http = _FakeHttp(route=_route(send=SEND_REFUSED))
    client = GacClient("AU", http, limiter=_limiter())

    async def run():
        await client.start_captcha()
        with pytest.raises(LoginError) as caught:
            await client.request_sms("0412345678", 100)
        assert "could not send SMS code" in str(caught.value)
        assert not isinstance(caught.value, CaptchaError)

    asyncio.run(run())
    # the puzzle was fetched and its check passed; only the code send was refused,
    # and the code was never checked, so no verify-code ticket was consumed
    urls = http.paths
    assert sum("captcha/v1/get/sec" in u for u in urls) == 1
    assert sum("captcha/v1/check/sec" in u for u in urls) == 1
    assert sum("send-verify-code" in u for u in urls) == 1
    assert not any("check-verify-code" in u for u in urls)
