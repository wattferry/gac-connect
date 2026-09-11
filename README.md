# GAC Connect

> **⚠️ BETA — not fully tested, use entirely at your own risk.** This is an
> unofficial client with no relationship to GAC. Expect bugs; things that
> worked yesterday may not work tomorrow. Remote commands physically act on your
> vehicle (A/C, locks, windows, tailgate, charging) and may misbehave or fail;
> using it alongside the official app may sign one of them out. Nothing here is
> warranted to work, keep working, or be safe. Review what an automation can do
> before you let it touch the car.

An unofficial async Python client for **GAC / Aion international** connected-car
accounts — read your vehicle's status and control charging from your own code.

> This project is not affiliated with, endorsed by, or supported by GAC or its
> affiliates. GAC and AION are third-party trademarks of their respective
> owners. Use it with a vehicle you own, on your own account.

Supports **Australia and New Zealand**. Other regions are listed as best-effort —
reports welcome.

## Install

```bash
pip install gac-connect          # library
pip install "gac-connect[cli]"   # + the `gac` command
```

## Library

```python
import aiohttp
from gac_connect import GacClient  # noqa

async def main():
    async with aiohttp.ClientSession() as http:
        client = GacClient("AU", http)

        # First run: sign in. You solve a slide puzzle and enter an SMS code.
        captcha = await client.start_captcha()
        # ... show captcha.background / captcha.piece, get the slide offset x ...
        await client.request_sms("04xxxxxxxx", x)
        await client.login_sms("04xxxxxxxx", "123456")

        vehicles = await client.list_vehicles()
        status = await client.get_status(vehicles[0].vin)
        print(status.soc, status.range_km, status.odometer_km)

        await client.charge_pause(vehicles[0].vin)
```

Pass a `TokenStore` to persist the session (the refresh token is single-use and
rotates on every refresh, so persistence matters):

```python
from gac_connect.session import FileStore
client = GacClient("AU", http, FileStore("~/.config/gac-connect/session.json"))
await client.load()
```

## CLI

```bash
gac login --mobile 04xxxxxxxx     # opens a browser puzzle, then asks for the SMS code
gac vehicles
gac status <VIN>
gac charge <VIN> pause            # or: now
```

## What you can read

State of charge, range, odometer, charging state and mode, plugged-in, cabin
temperature, 12 V battery, GPS, doors / windows / boot / lock state, per-tyre
pressure (in kPa) and temperature, and, where fitted, the fridge's mode, target
temperature and keep-running setting (`status.fridge_mode`, `status.fridge_temp_c`,
`status.fridge_keep_mode`, `status.fridge_keep_minutes`).

## Fridge

On cars with a fridge / warmer box:

```python
await client.fridge_on(vin, mode="refrigerate", temperature=3)   # 0 to 20 °C
await client.fridge_on(vin, mode="heat", temperature=42)         # 35 to 50 °C
await client.fridge_on(vin, mode="freeze", temperature=-12)      # -15 to -1 °C
await client.fridge_off(vin)
```

Each temperature is checked against the mode's range, then rounded to a whole
degree; bad values raise `ValueError` before anything is sent. Without a
temperature, this library's default for the mode is sent (refrigerate 3, heat 42,
freeze -12 °C), which also resets the target if the fridge is already running.
Calling `fridge_on` while the fridge runs changes its mode or temperature.

An accepted request is not proof the car applied it; check `status.fridge_mode`.

How long the fridge keeps running after you leave the car is a setting in the car.
`status.fridge_keep_mode` reports it as `"timed"` or `"unlimited"`. For a timed
setting, `status.fridge_keep_minutes` reports the remaining minutes supplied by the
service, which count down once you leave the car, or `None` when unavailable or
invalid. It is always `None` for unlimited or unknown settings.

## Command results

Commands are accepted asynchronously and their results are delivered by the
service over a separate channel. `PushClient` keeps that channel open and hands
each result (outcome, command event, session id) to your handler:

```python
from gac_connect import PushClient

def on_result(topic, payload, result):
    print(result.event, result.ok, result.session_id)

# The service's broker may be plain TCP; its credentials are short-lived, but
# you must opt in to sending them unencrypted.
push = PushClient(client.mqtt_info, on_result, decrypt=client.decrypt_push, allow_plaintext=True)
task = asyncio.create_task(push.run())   # reconnects on its own until stop()
...
push.stop()
await task
```

## Charging control

`charge_now`, `charge_pause`, and `set_charge_window` gate charging through the
car's schedule. The car applies commands asynchronously: pausing usually takes
effect quickly, resuming can take several minutes and may be delayed or fail — so
pause only for sustained periods.

## Request limits

Every request the library sends passes one limiter shared by all clients in the
process (`gac_connect.DEFAULT_LIMITER`); a client given its own `Limiter` is held
to both, so creating clients never resets the limits. Requests go out one at a
time, even across threads, at least a second apart, and no more than 20 a minute,
240 an hour and 3000 a day (`const.REQUEST_BUDGETS`, rolling windows, each
request counted when it finishes). Vehicle commands also have their own budget of
6 a minute and 60 an hour (`const.COMMAND_BUDGETS`). If the service answers "too
many requests", nothing is sent for at least 60 seconds, or for its `Retry-After`
(seconds or a date) up to a day. Past any of these the client raises
`RateLimitedError` (with `retry_after` in seconds) without sending anything.

The shared limiter keeps its state in a small file, locked while each request
runs, so every program and every run on the machine shares one budget and one
pause: a script restarted in a loop is limited like one that keeps running. The
file lives in your cache folder (`~/.cache/gac-connect/limits.json` on Linux,
`~/Library/Caches/gac-connect/` on macOS, `%LOCALAPPDATA%\gac-connect\` on
Windows) or wherever `GAC_CONNECT_LIMITS` points. If it cannot be read or written,
nothing is sent. If it is corrupt, a copy is kept (`limits.corrupt`) and requests
pause for a day, since any pause recorded in it is unknown; delete the file to reset
it sooner. A host that stores the state itself can set
`DEFAULT_LIMITER.state_path = None` before its first request and use
`export_state()` / `import_state()` instead (the Home Assistant integration does).

What is counted: requests this library sends on one machine. Other machines, the
official app, and the push channel's broker connection are not counted. A poll
every few minutes is plenty for most uses.

## Notes and limits

- Sign-in needs a human: a slide puzzle and an SMS code. There is no headless login.
- Locking is available; unlock, remote power and charger-release need a
  remote-control PIN that this client does not yet support, so those commands
  are refused.
- Using the official app and this client on the same account at the same time can
  occasionally sign one of them out.

## Home Assistant

A Home Assistant integration built on this library is available separately.

## Acknowledgements

This project stands on the shoulders of the community projects that brought other
EV brands into Home Assistant and Python, among them:

- [AwangYes/BYD-re](https://github.com/AwangYes/BYD-re) — BYD
- [Hyundai-Kia-Connect/kia_uvo](https://github.com/Hyundai-Kia-Connect/kia_uvo) — Hyundai / Kia
- [bimmerconnected/bimmer_connected](https://github.com/bimmerconnected/bimmer_connected) — BMW / Mini
- [SAIC-iSmart-API/saic-python-client-ng](https://github.com/SAIC-iSmart-API/saic-python-client-ng) — MG / SAIC
- [kvanbiesen/bmw-cardata-ha](https://github.com/kvanbiesen/bmw-cardata-ha) — BMW CarData

Thanks to their authors for showing what a good community integration looks like.

## Changes

- **0.2.0b9** — request limits: one `Limiter` shared by every client in a process
  (and kept between CLI runs), sending one request at a time with rolling budgets
  per minute, hour and day, a separate command budget, and a pause after any 429
  answer (Retry-After as seconds or a date, up to a day); requests over a limit
  raise `RateLimitedError` without being sent. The shared limiter's state is kept
  in a file in the user's cache folder, so separate runs share it; a client's own
  limiter applies on top of it. Limiter state can be exported and restored. Redirects are
  not followed. Token refreshes are coordinated between clients, rotated tokens are
  saved before use, and a session that can no longer refresh is not retried until
  a new session is stored.

- **0.2.0b8** — the fridge's keep-running setting in the vehicle status:
  `fridge_keep_mode` (timed or unlimited) and `fridge_keep_minutes` (time left).

- **0.2.0b7** — fridge / warmer box: `fridge_on(mode, temperature)` and
  `fridge_off()`, with per-mode temperature ranges checked before sending, and
  the fridge's mode and target temperature in the vehicle status.

## License

MIT.
