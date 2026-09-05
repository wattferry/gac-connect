"""Vehicle command catalog and request-body assembly.

Each command names a gateway path, a body ``kind``, whether it needs the
remote-control PIN, and whether it is in the ``stable`` support tier (the rest
are best-effort and may not apply to every model). The body shapes:

* ``read`` / ``flat`` → ``{"vin": …, **overrides}``
* ``ops``            → ``{"identifier": {"vin": …}, "operations": [{…ops, **overrides}]}``
* ``anti``           → ``{"identifier": {"vin": …}, "currentDuration": 30, **overrides}``

PIN-gated commands are listed for completeness but the client refuses them until
PIN support lands.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .const import IOV_PREFIX

_AIR = {"enableAutoMode": 1, "temperature": 24.0, "temperatureUnit": 0, "currentDuration": 30,
        "blowingMode": -1, "cyclingMode": -1, "enableAirCompressor": -1}


@dataclass(frozen=True)
class Command:
    name: str
    path: str
    kind: str              # read | flat | ops | anti
    pin: bool = False
    stable: bool = False
    category: str = ""
    description: str = ""
    ops: dict = field(default_factory=dict)


def _p(suffix: str) -> str:
    return f"{IOV_PREFIX}/{suffix}"


CATALOG: dict[str, Command] = {c.name: c for c in [
    # reads
    Command("status", _p("get_vehicle_status"), "read", stable=True, category="read", description="full telemetry"),
    Command("position", _p("get_position_status"), "read", stable=True, category="read", description="location"),
    # climate
    Command("aircon-on", _p("air_condition/open"), "ops", stable=True, category="climate",
            description="A/C on", ops={**_AIR, "airOperationType": "on"}),
    Command("aircon-off", _p("air_condition/open"), "ops", stable=True, category="climate",
            description="A/C off", ops={**_AIR, "airOperationType": "off"}),
    Command("ventilate-on", _p("ventilate_mode/open"), "flat", stable=True, category="climate",
            description="cabin ventilation on"),
    Command("ventilate-off", _p("ventilate_mode/close"), "flat", category="climate", description="cabin ventilation off"),
    Command("steering-on", _p("steering/on"), "flat", stable=True, category="climate", description="steering heat on"),
    Command("steering-off", _p("steering/off"), "flat", stable=True, category="climate", description="steering heat off"),
    # body
    Command("lock", _p("door/lock"), "flat", stable=True, category="body", description="lock doors"),
    Command("unlock", _p("door/unlock"), "flat", pin=True, category="body", description="unlock doors (PIN)"),
    Command("window-open", _p("window/open"), "flat", category="body", description="windows open"),
    Command("window-close", _p("window/close"), "flat", stable=True, category="body", description="windows close"),
    Command("sunroof-open", _p("sunroof/open"), "flat", category="body", description="sunroof open"),
    Command("sunroof-close", _p("sunroof/close"), "flat", category="body", description="sunroof close"),
    Command("tailgate-open", _p("hatchback/open"), "flat", category="body", description="tailgate open"),
    Command("tailgate-close", _p("hatchback/close"), "flat", category="body", description="tailgate close"),
    Command("flash-on", _p("flash/on"), "flat", category="body", description="flash lights on"),
    Command("flash-off", _p("flash/off"), "flat", category="body", description="flash lights off"),
    Command("horn-on", _p("horn/on"), "flat", category="body", description="sound horn"),
    Command("horn-off", _p("horn/off"), "flat", category="body", description="horn off"),
    # charging & power
    Command("charge-schedule", _p("charging/reservation"), "ops", stable=True, category="charge", description="set charge schedule"),
    Command("charger-unlock", _p("charger/unlock"), "flat", pin=True, category="charge",
            description="release charging gun / stop charging (PIN)"),
    Command("battery-precondition", _p("battery/pretreatment"), "ops", category="charge",
            description="battery preconditioning", ops={"batteryPretreatmentMode": 1}),
    Command("power-on", _p("engine/start"), "flat", pin=True, category="power", description="remote power on (PIN)"),
    Command("power-off", _p("engine/stop"), "flat", pin=True, category="power", description="remote power off (PIN)"),
    Command("powerkeep-stop", _p("stop/powerKeep"), "flat", stable=True, category="power", description="stop power-keep"),
]}


CLIMATE_TEMP_RANGE = (18.0, 32.0)   # °C
CLIMATE_MINUTES_RANGE = (5, 60)


def validate_climate(temperature: Any, minutes: Any) -> tuple[float, int]:
    """Check A/C inputs before anything is sent to the car; raises ValueError."""
    try:
        t = float(temperature)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("temperature must be a number") from None
    if not math.isfinite(t):
        raise ValueError("temperature must be finite")
    lo, hi = CLIMATE_TEMP_RANGE
    if not lo <= t <= hi:
        raise ValueError(f"temperature must be between {lo:g} and {hi:g} °C")
    try:
        mf = float(minutes)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("minutes must be an integer") from None
    if not math.isfinite(mf) or mf != int(mf):
        raise ValueError("minutes must be a whole number")
    m = int(mf)
    mlo, mhi = CLIMATE_MINUTES_RANGE
    if not mlo <= m <= mhi:
        raise ValueError(f"minutes must be between {mlo} and {mhi}")
    return round(t * 2) / 2, m


def build_body(cmd: Command, vin: str, overrides: dict[str, Any] | None = None) -> dict:
    overrides = overrides or {}
    if cmd.kind in ("read", "flat"):
        return {"vin": vin, **overrides}
    if cmd.kind == "anti":
        return {"identifier": {"vin": vin}, "currentDuration": 30, **overrides}
    if cmd.kind == "ops":
        return {"identifier": {"vin": vin}, "operations": [{**cmd.ops, **overrides}]}
    raise ValueError(f"unknown command kind {cmd.kind!r}")
