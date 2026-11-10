from unittest.mock import AsyncMock, patch

import pytest

from gac_connect.errors import PinRequiredError
from gac_connect.push import PushResult


@pytest.fixture
def cli_client(monkeypatch):
    pytest.importorskip("typer")
    from gac_connect import cli

    client = AsyncMock()
    monkeypatch.setattr(cli, "_client", lambda *args, **kwargs: client)
    return cli, client


def test_cli_securely_prompts_and_waits_for_success(cli_client):
    from typer.testing import CliRunner

    cli, client = cli_client
    client.unlock_charger.return_value = {"code": 13001, "data": {"sessionId": "unlock-1"}}
    client.poll_command_result.return_value = PushResult(True, "0", None, {})
    result = CliRunner().invoke(
        cli.app,
        ["charger-unlock", "VIN", "--timeout", "12", "--poll-interval", "3"],
        input="246810\n",
    )

    assert result.exit_code == 0
    assert "charger unlock: success (session unlock-1)" in result.output
    assert "246810" not in result.output
    client.load.assert_awaited_once()
    client.unlock_charger.assert_awaited_once_with("VIN", "246810")
    client.poll_command_result.assert_awaited_once_with("VIN", "unlock-1")


def test_cli_no_wait_submits_once_without_polling(cli_client):
    from typer.testing import CliRunner

    cli, client = cli_client
    client.unlock_charger.return_value = {"code": 13001, "data": {"sessionId": "unlock-2"}}
    result = CliRunner().invoke(cli.app, ["charger-unlock", "VIN", "--no-wait"], input="135790\n")

    assert result.exit_code == 0
    assert "accepted (session unlock-2); result not awaited" in result.output
    assert "135790" not in result.output
    client.unlock_charger.assert_awaited_once_with("VIN", "135790")
    client.poll_command_result.assert_not_awaited()


@pytest.mark.parametrize("acknowledgement,terminal,exit_code,text", [
    ({"code": 13001}, None, 2, "pending"),
    ({"code": 13001, "data": {"sessionId": "unlock-3"}},
     PushResult(False, "13005", None, {}, reason="vehicle conditions not met"), 1, "failed"),
])
def test_cli_non_success_results_have_nonzero_exit_status(
    cli_client, acknowledgement, terminal, exit_code, text,
):
    from typer.testing import CliRunner

    cli, client = cli_client
    client.unlock_charger.return_value = acknowledgement
    client.poll_command_result.return_value = terminal
    result = CliRunner().invoke(cli.app, ["charger-unlock", "VIN"], input="246810\n")

    assert result.exit_code == exit_code
    assert text in result.output and "246810" not in result.output
    client.unlock_charger.assert_awaited_once_with("VIN", "246810")


def test_cli_timeout_does_not_repeat_the_unlock_command(cli_client):
    from typer.testing import CliRunner

    cli, client = cli_client
    client.unlock_charger.return_value = {"code": 13001, "data": {"sessionId": "unlock-4"}}
    wait = AsyncMock(return_value=cli._CommandOutcome(cli._CommandState.TIMEOUT, "unlock-4"))
    with patch("gac_connect.cli._wait_for_command", new=wait):
        result = CliRunner().invoke(
            cli.app, ["charger-unlock", "VIN", "--timeout", "0.5"], input="246810\n",
        )

    assert result.exit_code == 2 and "timeout" in result.output
    client.unlock_charger.assert_awaited_once_with("VIN", "246810")
    wait.assert_awaited_once_with(
        client, "VIN", {"code": 13001, "data": {"sessionId": "unlock-4"}},
        timeout=0.5, poll_interval=5.0,
    )


def test_cli_reports_a_rejected_pin_without_disclosing_it(cli_client):
    from typer.testing import CliRunner

    cli, client = cli_client
    client.unlock_charger.side_effect = PinRequiredError("private", code="11018")
    result = CliRunner().invoke(cli.app, ["charger-unlock", "VIN"], input="246810\n")

    assert result.exit_code == 1
    assert "PIN rejected (code 11018)" in result.output
    assert "246810" not in result.output and "private" not in result.output


@pytest.mark.parametrize("command,method", [("power-on", "power_on"), ("power-off", "power_off")])
def test_cli_power_commands_prompt_securely_and_submit_once(cli_client, command, method):
    from typer.testing import CliRunner

    cli, client = cli_client
    getattr(client, method).return_value = {"code": 13001, "data": {"sessionId": command}}
    result = CliRunner().invoke(cli.app, [command, "VIN"], input="246810\n")

    assert result.exit_code == 0
    assert f"vehicle {command}: accepted (session {command})" in result.output
    assert "246810" not in result.output
    getattr(client, method).assert_awaited_once_with("VIN", "246810")
    other = client.power_off if method == "power_on" else client.power_on
    other.assert_not_awaited()


@pytest.mark.parametrize("command,method", [("power-on", "power_on"), ("power-off", "power_off")])
def test_cli_power_pin_rejection_is_sanitized(cli_client, command, method):
    from typer.testing import CliRunner

    cli, client = cli_client
    getattr(client, method).side_effect = PinRequiredError("private", code="11018")
    result = CliRunner().invoke(cli.app, [command, "VIN"], input="246810\n")

    assert result.exit_code == 1
    assert f"vehicle {command}: PIN rejected (code 11018)" in result.output
    assert "246810" not in result.output and "private" not in result.output
