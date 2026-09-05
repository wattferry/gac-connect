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
temperature, 12 V battery, GPS, doors / windows / boot / lock state, and per-tyre
pressure (in kPa) and temperature.

## Charging control

`charge_now`, `charge_pause`, and `set_charge_window` gate charging through the
car's schedule. The car applies commands asynchronously: pausing usually takes
effect quickly, resuming can take several minutes and may be delayed or fail — so
pause only for sustained periods.

## Notes and limits

- Sign-in needs a human: a slide puzzle and an SMS code. There is no headless login.
- Locking is available; unlock, remote power and charger-release need a
  remote-control PIN that this client does not yet support, so those commands
  are refused.
- Using the official app and this client on the same account at the same time can
  occasionally sign one of them out.

## Home Assistant

A Home Assistant integration built on this library is available separately.

## License

MIT.
