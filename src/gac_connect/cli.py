"""Command-line interface: ``gac login | status | vehicles | charge | charger``.

Installed with the ``[cli]`` extra. Stores its session in
``~/.config/gac-connect/session.json`` (0600). The block puzzle opens in a local
browser page during ``gac login``.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import typer
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("the CLI needs the extra: pip install 'gac-connect[cli]'") from exc

import aiohttp

from gac_connect import puzzle
from gac_connect.client import GacClient, command_session_id
from gac_connect.errors import PinRequiredError
from gac_connect.push import PushResult
from gac_connect.puzzle import PuzzleSession
from gac_connect.session import FileStore

app = typer.Typer(add_completion=False, help="Unofficial GAC / Aion client.")
SESSION = Path.home() / ".config" / "gac-connect" / "session.json"

class _PuzzlePage:
    """Serves the shared puzzle page on 127.0.0.1 and hands each Verify to the session."""

    def __init__(self, session: PuzzleSession, loop: asyncio.AbstractEventLoop) -> None:
        html = puzzle.page("the terminal").encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _reply(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("content-type", kind)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if "state=1" in self.path:
                    state = session.state()
                    self._reply(200, json.dumps(state).encode(), "application/json")
                else:
                    self._reply(200, html, "text/html; charset=utf-8")

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                    x = puzzle.offset(data.get("x") if isinstance(data, dict) else None)
                except ValueError:
                    self._reply(400, b'{"ok": false}', "application/json")
                    return
                result = asyncio.run_coroutine_threadsafe(session.submit(x), loop).result()
                self._reply(409 if result.get("busy") else 200, json.dumps(result).encode(), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"


async def _run(coro):
    return await coro


def _client(http: aiohttp.ClientSession, region: str, timezone: str | None = None) -> GacClient:
    # the shared limiter keeps its state between runs, so a loop around the CLI is limited too
    return GacClient(region, http, FileStore(SESSION), timezone=timezone)


class _CommandState(StrEnum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class _CommandOutcome:
    state: _CommandState
    session_id: str | None = None
    result: PushResult | None = None


async def _wait_for_command(client: GacClient, vin: str, acknowledgement: object, *,
                            timeout: float, poll_interval: float) -> _CommandOutcome:
    """CLI-only bounded polling; command submission remains a separate library operation."""
    session_id = command_session_id(acknowledgement)
    if session_id is None or timeout == 0:
        return _CommandOutcome(_CommandState.PENDING, session_id)
    deadline = time.monotonic() + timeout
    latest = None
    while True:
        latest = await client.poll_command_result(vin, session_id)
        if latest is not None and latest.ok is not None:
            state = _CommandState.SUCCESS if latest.ok else _CommandState.FAILURE
            return _CommandOutcome(state, session_id, latest)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _CommandOutcome(_CommandState.TIMEOUT, session_id, latest)
        await asyncio.sleep(min(poll_interval, remaining))


_PinCommand = Callable[[GacClient], Callable[[str, str], Awaitable[object]]]


async def _execute_pin_command(
    client: GacClient,
    vin: str,
    pin: str,
    submit: _PinCommand,
    *,
    timeout: float | None,
    poll_interval: float,
) -> _CommandOutcome:
    acknowledgement = await submit(client)(vin, pin)
    if timeout is None:
        return _CommandOutcome(_CommandState.ACCEPTED, command_session_id(acknowledgement))
    return await _wait_for_command(
        client, vin, acknowledgement, timeout=timeout, poll_interval=poll_interval,
    )


def _report_pin_command(label: str, outcome: _CommandOutcome, accepted_note: str = "") -> None:
    suffix = f" (session {outcome.session_id})" if outcome.session_id else ""
    if outcome.state is _CommandState.ACCEPTED:
        typer.echo(f"{label}: accepted{suffix}{accepted_note}")
        return
    if outcome.state is _CommandState.SUCCESS:
        typer.echo(f"{label}: success{suffix}")
        return
    if outcome.state is _CommandState.FAILURE:
        code = outcome.result.code if outcome.result else None
        reason = outcome.result.reason if outcome.result else None
        detail = f": {reason}" if reason else (f" (code {code})" if code else "")
        typer.echo(f"{label}: failed{suffix}{detail}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{label}: {outcome.state.value}{suffix}; physical outcome unknown", err=True)
    raise typer.Exit(2)


def _run_pin_command(
    vin: str,
    pin: str,
    region: str,
    label: str,
    submit: _PinCommand,
    *,
    timeout: float | None = None,
    poll_interval: float = 5.0,
    accepted_note: str = "",
) -> None:
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region)
            await client.load()
            try:
                outcome = await _execute_pin_command(
                    client, vin, pin, submit, timeout=timeout, poll_interval=poll_interval,
                )
            except PinRequiredError as exc:
                suffix = f" (code {exc.code})" if exc.code else ""
                typer.echo(f"{label}: PIN rejected{suffix}", err=True)
                raise typer.Exit(1) from None
            _report_pin_command(label, outcome, accepted_note)

    asyncio.run(go())


@app.command()
def login(mobile: str = typer.Option(..., prompt=True), region: str = "AU") -> None:
    """Sign in with your mobile number and an SMS code."""
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region)
            session = PuzzleSession(client, mobile, await client.start_captcha())
            pz = _PuzzlePage(session, asyncio.get_running_loop())
            webbrowser.open(pz.url)
            typer.echo(f"Solve the puzzle: {pz.url}")
            while session.status == "pending":
                await asyncio.sleep(0.3)
            pz.httpd.shutdown()
            if session.status == "failed":
                typer.echo(session.message)
                raise typer.Exit(1)
            code = await asyncio.to_thread(typer.prompt, "SMS code")
            await client.login_sms(mobile, code)
            typer.echo("Signed in.")
    asyncio.run(go())


@app.command("power-on")
def vehicle_power_on(vin: str, region: str = "AU") -> None:
    """Verify the remote-control PIN and submit one remote power-on command."""
    pin = typer.prompt("Remote-control PIN", hide_input=True)
    _run_pin_command(vin, pin, region, "vehicle power-on", lambda client: client.power_on)


@app.command("power-off")
def vehicle_power_off(vin: str, region: str = "AU") -> None:
    """Verify the PIN and stop power previously started remotely."""
    pin = typer.prompt("Remote-control PIN", hide_input=True)
    _run_pin_command(vin, pin, region, "vehicle power-off", lambda client: client.power_off)


@app.command()
def vehicles(region: str = "AU") -> None:
    """List the vehicles on the account."""
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region)
            await client.load()
            for v in await client.list_vehicles():
                typer.echo(f"{v.vin}  {v.model or ''}  {v.plate or ''}")
    asyncio.run(go())


@app.command()
def status(vin: str, region: str = "AU") -> None:
    """Show live status for a VIN."""
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region)
            await client.load()
            s = await client.get_status(vin)
            typer.echo(json.dumps({
                "soc": s.soc, "range_km": s.range_km, "odometer_km": s.odometer_km,
                "charging": s.charging, "plugged_in": s.plugged_in, "locked": s.locked,
                "cabin_temp_c": s.cabin_temp_c, "aux_voltage": s.aux_voltage,
                "tyres_kpa": [t.pressure_kpa for t in s.tyres],
            }, indent=2))
    asyncio.run(go())


@app.command()
def charge(vin: str, action: str, region: str = "AU", timezone: str = "") -> None:
    """Charge control: action = now. There is no pause; a charge window blocks AC charging outside it."""
    if action != "now":
        raise typer.BadParameter("action must be now; there is no pause, set a charge window instead")
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region, timezone or None)
            await client.load()
            await client.charge_now(vin)
            typer.echo(f"charge {action}: accepted")
    asyncio.run(go())


@app.command("charger-unlock")
def charger_unlock(
    vin: str,
    region: str = "AU",
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for the vehicle's result."),
    timeout: float = typer.Option(45.0, min=0.0, help="Maximum seconds to wait."),
    poll_interval: float = typer.Option(5.0, min=0.1, help="Seconds between result polls."),
) -> None:
    """Verify the remote-control PIN and unlock the charging connector once."""
    pin = typer.prompt("Remote-control PIN", hide_input=True)
    _run_pin_command(
        vin,
        pin,
        region,
        "charger unlock",
        lambda client: client.unlock_charger,
        timeout=timeout if wait else None,
        poll_interval=poll_interval,
        accepted_note="; result not awaited" if not wait else "",
    )


if __name__ == "__main__":
    app()
