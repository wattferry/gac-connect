"""Typed vehicle state, decoded from the gateway's accessor groups.

``get_vehicle_status`` returns ``data.results[0]`` as a bag of ``*Accessors``
lists. This module folds the useful ones into a flat, typed :class:`VehicleStatus`
with the gateway's quirks resolved:

* SoC is reported 0..1, exposed as a percentage;
* a charging current of 1638.0 is the gateway's "unknown" placeholder → None;
* tyre pressure stays in native kPa (let the consumer convert to psi if it wants);
* lock uses the gateway's inverted polarity (0 = locked), decoded to a bool;
* openMode 0 = closed, >0 = open, -1 = not fitted.

The raw ``results`` dict is retained on ``.raw`` so anything not modelled here is
still reachable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ChargingStatus(IntEnum):
    NOT_CHARGING = 0
    CHARGING = 2
    UNKNOWN = -1


class ChargingMode(IntEnum):
    FREE = 0        # charge whenever plugged in
    SCHEDULED = 1   # only inside the reservation window
    UNKNOWN = -1


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _first(res: dict, key: str) -> dict:
    v = res.get(key) or [{}]
    return v[0] if v else {}


def _is_open(v: Any) -> bool:
    return (_num(v) or 0) > 0


@dataclass
class Tyre:
    position: str
    pressure_kpa: float | None
    temperature_c: float | None


@dataclass
class VehicleStatus:
    online: bool | None = None
    updated_ms: int | None = None
    # battery / charging
    soc: float | None = None                 # %
    range_km: float | None = None
    charging: bool | None = None
    charging_mode: ChargingMode | None = None
    charge_current_a: float | None = None
    estimated_charge_minutes: int | None = None
    plugged_in: bool | None = None
    charger_locked: bool | None = None
    # odometer / environment
    odometer_km: float | None = None
    cabin_temp_c: float | None = None
    pm25: float | None = None
    aux_voltage: float | None = None         # 12 V battery
    # location
    latitude: float | None = None
    longitude: float | None = None
    # body
    locked: bool | None = None               # True = all doors locked
    door_open: bool | None = None
    window_open: bool | None = None
    hatch_open: bool | None = None
    sunroof_open: bool | None = None
    tyres: list[Tyre] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    # gateway "unknown" placeholder for charge current
    _CURRENT_UNKNOWN = 1638.0
    # doorAccessors[].lock: 0 = locked, 1 = unlocked, <0 = unknown
    _LOCK_LOCKED = 0
    # tyre report order (positions unverified; documented as such)
    _TYRE_POS = {1: "front left", 2: "front right", 3: "rear left", 4: "rear right"}

    @classmethod
    def from_results(cls, results: dict, *, online: bool | None = None, updated_ms: int | None = None) -> VehicleStatus:
        drv = _first(results, "drivingOverviewAccessors")
        chg = _first(results, "chargingAccessors")
        cgr = _first(results, "chargerAccessors")
        env = _first(results, "environmentAccessors")
        bat = _first(results, "batteryAccessors")
        pos = _first(results, "positionAccessors")

        soc = _num(drv.get("remainElectricityPercentage"))
        cur = _num(chg.get("chargingCurrent"))
        if cur == cls._CURRENT_UNKNOWN:
            cur = None
        est = _num(chg.get("estimatedChargedDuration"))

        doors = results.get("doorAccessors") or []
        locked = None
        if doors:
            locked = all(d.get("lock") == cls._LOCK_LOCKED for d in doors)

        tyres = []
        for t in results.get("tyreAccessors") or []:
            idx = t.get("tyre")
            tyres.append(Tyre(
                position=cls._TYRE_POS.get(idx, str(idx)),
                pressure_kpa=_num(t.get("pressure")) if t.get("pressureFlag", 1) else None,
                temperature_c=_num(t.get("temperature")) if t.get("temperatureFlag", 1) else None,
            ))

        def any_open(key: str, sub: str) -> bool | None:
            items = results.get(key)
            if not items:
                return None
            return any(_is_open(i.get(sub)) for i in items)

        raw_status = _num(chg.get("chargingStatus"))
        try:
            mode = ChargingMode(int(_num(chg.get("chargingMode")))) if chg.get("chargingMode") is not None else None
        except ValueError:
            mode = ChargingMode.UNKNOWN

        return cls(
            online=online,
            updated_ms=updated_ms,
            soc=round(soc * 100, 1) if soc is not None else None,
            range_km=_num(drv.get("remainVoyageCourse")),
            charging=(raw_status == ChargingStatus.CHARGING) if raw_status is not None else None,
            charging_mode=mode,
            charge_current_a=cur,
            estimated_charge_minutes=int(est) if est is not None else None,
            plugged_in=(cgr.get("plug") == 1) if cgr.get("plug") is not None else None,
            charger_locked=(cgr.get("lock") == cls._LOCK_LOCKED) if cgr.get("lock") is not None else None,
            odometer_km=_num(drv.get("totalVoyageCourse")),
            cabin_temp_c=_num(env.get("temperatureInsideCar")),
            pm25=_num(env.get("pm25")),
            aux_voltage=_num(bat.get("voltage")),
            latitude=_num(pos.get("latitude")),
            longitude=_num(pos.get("longitude")),
            locked=locked,
            door_open=any_open("doorAccessors", "openMode"),
            window_open=any_open("windowAccessors", "openMode"),
            hatch_open=any_open("hatchAccessors", "openMode"),
            sunroof_open=any_open("sunroofAccessors", "openMode"),
            tyres=tyres,
            raw=results,
        )


@dataclass
class Vehicle:
    vin: str
    plate: str | None = None
    model: str | None = None
    color: str | None = None
    default: bool = False

    @classmethod
    def from_record(cls, rec: dict) -> Vehicle:
        return cls(
            vin=rec.get("vin") or rec.get("vinCode") or "",
            plate=rec.get("plateNo"),
            model=rec.get("vehStyleName") or rec.get("vehicleType"),
            color=rec.get("vehicleColor"),
            default=bool(rec.get("defaultFlag")),
        )
