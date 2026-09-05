"""Offline unit tests: model decoding, command bodies, charge levers, session,
and that the client constructs. No network, no secrets beyond _material.pem."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_imports():
    import gac_connect  # noqa: F401
    from gac_connect import client, commands, models, vehicle, auth, session  # noqa: F401


def test_status_decoding():
    from gac_connect.models import ChargingMode, VehicleStatus
    results = {
        "drivingOverviewAccessors": [{"remainElectricityPercentage": 0.89,
                                      "remainVoyageCourse": 454.0, "totalVoyageCourse": 3205.0}],
        "chargingAccessors": [{"chargingStatus": 2, "chargingMode": 1,
                               "chargingCurrent": 1638.0, "estimatedChargedDuration": 0}],
        "chargerAccessors": [{"plug": 1, "lock": 0}],
        "environmentAccessors": [{"temperatureInsideCar": 30.0, "pm25": 0}],
        "batteryAccessors": [{"voltage": 12.4}],
        "positionAccessors": [{"latitude": 1.5, "longitude": 100.5}],
        "doorAccessors": [{"door": 1, "openMode": 0, "lock": 0},
                          {"door": 2, "openMode": 0, "lock": 0}],
        "windowAccessors": [{"window": 1, "openMode": 0}],
        "tyreAccessors": [{"tyre": 1, "pressure": 254.0, "temperature": 25.0,
                           "pressureFlag": 1, "temperatureFlag": 1}],
    }
    s = VehicleStatus.from_results(results, online=True, updated_ms=123)
    assert s.soc == 89.0
    assert s.range_km == 454.0 and s.odometer_km == 3205.0
    assert s.charging is True and s.charging_mode is ChargingMode.SCHEDULED
    assert s.charge_current_a is None       # 1638 placeholder -> None
    assert s.plugged_in is True and s.locked is True
    assert s.door_open is False and s.aux_voltage == 12.4
    assert s.tyres[0].pressure_kpa == 254.0 and s.tyres[0].position == "front left"


def test_command_bodies():
    from gac_connect.commands import CATALOG, build_body
    assert build_body(CATALOG["status"], "V1") == {"vin": "V1"}
    ac = build_body(CATALOG["aircon-on"], "V1")
    assert ac["identifier"]["vin"] == "V1" and ac["operations"][0]["airOperationType"] == "on"
    assert CATALOG["unlock"].pin is True and CATALOG["lock"].pin is False


def test_charge_operations():
    from zoneinfo import ZoneInfo
    from gac_connect import vehicle
    now = vehicle.charge_now_operation()
    assert now["chargingMode"] == vehicle.MODE_FREE
    pause = vehicle.pause_operation(ZoneInfo("Australia/Brisbane"))
    assert pause["chargingMode"] == vehicle.MODE_SCHEDULED
    assert "startTime" in pause and "stopTime" in pause
    win = vehicle.window_operation("23:00", "05:00", ZoneInfo("Australia/Brisbane"), weekly=0)
    assert win["dailyReservationStartTime"] == (23 * 60) * 60000


def test_session_roundtrip():
    from gac_connect.session import Session
    s = Session(token="t", refresh_token="r", expire_time=10**14, rexpire_time=10**14, region="AU")
    assert s.access_valid and s.refresh_valid
    assert Session.from_dict(s.to_dict()).token == "t"
    assert not Session(token="t", expire_time=1).access_valid


@pytest.mark.asyncio
async def test_client_constructs():
    import aiohttp
    from gac_connect.client import GacClient
    from gac_connect.errors import RegionError
    try:
        async with aiohttp.ClientSession() as http:
            c = GacClient("AU", http)
            assert c.region == "AU"
            with pytest.raises(RegionError):
                GacClient("ZZ", http)
    except RuntimeError as exc:
        pytest.skip(str(exc))  # material missing


def test_captcha_proofs():
    from gac_connect.auth import Captcha
    # a synthetic secretKey (32B key @DS@ 16B iv) — proofs must be deterministic per x
    c = Captcha(background="", piece="", secret_key="K" * 32 + "@DS@" + "I" * 16, token="tok")
    assert c.point_json(100) == c.point_json(100)
    assert c.ticket(100) != c.point_json(100)
    assert len(c.ticket(100)) > 0


def test_status_climate_and_window():
    from gac_connect.models import VehicleStatus
    results = {
        "airConditionAccessors": [{"enableAirCompressor": 1, "windStrength": 3.0, "temperature": 21.5}],
        "steeringAccessors": [{"steering": 0}],
        "chargingAccessors": [{"dailyReservationStartTime": 82800000, "dailyReservationStopTime": 18000000,
                               "weeklyReservation": 0, "chargingMode": 1, "chargingStatus": 0}],
        "sunroofAccessors": [{"openMode": -1}],
    }
    s = VehicleStatus.from_results(results)
    assert s.ac_on is True and s.ac_target_temp_c == 21.5
    assert s.steering_heat_on is False
    assert (s.charge_window_start, s.charge_window_stop, s.charge_weekly) == ("23:00", "05:00", 0)
    assert s.sunroof_open is None   # -1 = unknown, not "closed"

    off = VehicleStatus.from_results({"airConditionAccessors": [{"enableAirCompressor": 0, "windStrength": 0.0}]})
    assert off.ac_on is False
    assert VehicleStatus.from_results({}).ac_on is None


def test_fitted_and_time_edges():
    from gac_connect.models import VehicleStatus, _hhmm
    s = VehicleStatus.from_results({
        "hatchAccessors": [{"openMode": -1, "hatch": 1}, {"openMode": 0, "hatch": 2}],
        "windowAccessors": [{"openMode": "2"}],
    })
    assert s.hatch_open is False        # one fitted + closed item -> closed
    assert s.window_open is True        # numeric strings decode
    assert VehicleStatus.from_results({"hatchAccessors": [{"openMode": "x"}]}).hatch_open is None
    assert _hhmm(0) == "00:00" and _hhmm(86_399_000) == "23:59"
    assert _hhmm(86_400_000) is None and _hhmm(-1) is None and _hhmm("abc") is None


def test_climate_validation():
    import pytest
    from gac_connect.commands import validate_climate
    assert validate_climate(21.3, 30) == (21.5, 30)
    assert validate_climate("24", "5") == (24.0, 5)
    for bad in ((float("nan"), 30), (float("inf"), 30), (17.9, 30), (32.1, 30), ("hot", 30),
                (22, 4), (22, 61), (22, -1), (22, "x"), (None, 30)):
        with pytest.raises(ValueError):
            validate_climate(*bad)


def test_climate_sentinels():
    from gac_connect.models import VehicleStatus
    def R(air=None, steer=None):
        groups = {"airConditionAccessors": [air] if air else None, "steeringAccessors": [steer] if steer else None}
        return VehicleStatus.from_results({k: v for k, v in groups.items() if v})
    assert R({"enableAirCompressor": 0, "windStrength": 0.0}).ac_on is False
    assert R({"enableAirCompressor": 1, "windStrength": 0.0}).ac_on is True
    assert R({"enableAirCompressor": -1, "windStrength": 2.0}).ac_on is True
    assert R({"enableAirCompressor": 0}).ac_on is None            # partial off -> unknown
    assert R({"enableAirCompressor": -1, "windStrength": -1}).ac_on is None
    assert R(steer={"steering": -1}).steering_heat_on is None
    assert R(steer={"steering": 0}).steering_heat_on is False
    assert R(steer={"steering": 1}).steering_heat_on is True


def test_climate_minutes_whole():
    import pytest
    from gac_connect.commands import validate_climate
    assert validate_climate(22, 30.0) == (22.0, 30)
    for bad in (5.9, "5.5", float("nan"), float("inf"), -float("inf"), 10**400):
        with pytest.raises(ValueError):
            validate_climate(22, bad)
    for bad_t in (float("inf"), 10**400):
        with pytest.raises(ValueError):
            validate_climate(bad_t, 30)
    from gac_connect.models import VehicleStatus
    assert VehicleStatus.from_results({"chargingAccessors": [{"weeklyReservation": float("nan")}]}).charge_weekly is None
    assert VehicleStatus.from_results({"chargingAccessors": [{"weeklyReservation": 2.5}]}).charge_weekly is None
    assert VehicleStatus.from_results({"chargingAccessors": [{"weeklyReservation": 3}]}).charge_weekly == 3


def test_lights_state():
    from gac_connect.models import VehicleStatus
    L = lambda v: VehicleStatus.from_results({"lightAccessors": [{"open": v, "light": 1}]}).lights_on  # noqa: E731
    assert L(1) is True and L(0) is False and L(-1) is None and L("x") is None
    assert VehicleStatus.from_results({}).lights_on is None
