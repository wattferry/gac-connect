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
