import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from gac_connect.commands import CATALOG
from gac_connect.const import IOV_PIN_CHECK
from gac_connect.errors import CommandError, PinRequiredError, ResponseError
from test_failures import Response, make_client


@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("response", [{"code": 13001}, {"success": True}, {"success": "false"}, {}])
def test_unlock_uses_normal_command_validation_without_a_pin(generic, response):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=response)

    async def run():
        await client.load()
        action = client.command("VIN", "unlock") if generic else client.unlock("VIN")
        if response.get("code") == 13001 or response.get("success") is True:
            assert await action == response
        else:
            with pytest.raises(CommandError):
                await action

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        CATALOG["unlock"].path, {"vin": "VIN"}, sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


@pytest.mark.parametrize("name", ["power-on", "power-off"])
def test_other_pin_guards_remain_in_force(name):
    client, http, _ = make_client(Response(), valid=True)
    with pytest.raises(PinRequiredError):
        asyncio.run(client.command("VIN", name))
    http.post.assert_not_called()


def test_generic_charger_unlock_requires_the_dedicated_pin_method():
    client, http, _ = make_client(Response(), valid=True)
    with pytest.raises(PinRequiredError):
        asyncio.run(client.command("VIN", "charger-unlock"))
    http.post.assert_not_called()
    assert CATALOG["charger-unlock"].pin is True
    assert CATALOG["charger-unlock"].path.endswith("/access_gateway/charger/unlock")


@pytest.mark.parametrize("response", [{"success": True}, {"code": "0000"}])
def test_pin_verification_returns_normally_on_success_without_exposing_the_pin(response, caplog):
    client, _, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=response)
    caplog.set_level(logging.DEBUG, logger="gac_connect.client")

    async def run():
        await client.load()
        return await client.verify_pin("246810")

    assert asyncio.run(run()) is None
    client._iov_call.assert_awaited_once_with(IOV_PIN_CHECK, {"pin": "246810"}, sensitive=None)
    assert "246810" not in caplog.text


def test_pin_verification_raises_a_sanitized_error_on_rejection(caplog):
    client, _, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"success": False, "code": 11018, "msg": "must not escape"})
    caplog.set_level(logging.DEBUG, logger="gac_connect.client")

    async def run():
        await client.load()
        return await client.verify_pin("246810")

    with pytest.raises(PinRequiredError) as caught:
        asyncio.run(run())
    assert caught.value.code == "11018"
    assert "246810" not in caplog.text
    assert "must not escape" not in str(caught.value)


@pytest.mark.parametrize("response", [None, [], {}, {"success": "true"}])
def test_malformed_pin_responses_are_not_accepted(response):
    client, _, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=response)

    async def run():
        await client.load()
        return await client.verify_pin("246810")

    with pytest.raises(ResponseError):
        asyncio.run(run())


def test_charger_unlock_verifies_then_sends_the_vin_once():
    client, _, _ = make_client(Response(), valid=True)
    client.verify_pin = AsyncMock(return_value=None)
    client._iov_call = AsyncMock(return_value={"code": 13001, "data": {"sessionId": "unlock-1"}})

    async def run():
        await client.load()
        return await client.unlock_charger("VIN", "246810")

    assert asyncio.run(run())["data"]["sessionId"] == "unlock-1"
    client.verify_pin.assert_awaited_once_with("246810")
    client._iov_call.assert_awaited_once_with(
        CATALOG["charger-unlock"].path, {"vin": "VIN"}, sensitive={"vin": "VIN"}, kind="command",
    )


def test_rejected_pin_never_sends_the_unlock_command():
    client, http, _ = make_client(Response(), valid=True)
    client.verify_pin = AsyncMock(side_effect=PinRequiredError("incorrect PIN", code="11018"))
    client._iov_call = AsyncMock()
    with pytest.raises(PinRequiredError) as caught:
        asyncio.run(client.unlock_charger("VIN", "246810"))
    assert caught.value.code == "11018"
    client._iov_call.assert_not_awaited()
    http.post.assert_not_called()


@pytest.mark.parametrize("vin,session_id", [("", "S1"), ("VIN", ""), (None, "S1")])
def test_poll_rejects_missing_identifiers_without_a_request(vin, session_id):
    client, http, _ = make_client(Response(), valid=True)
    with pytest.raises(ValueError):
        asyncio.run(client.poll_command_result(vin, session_id))
    http.post.assert_not_called()
