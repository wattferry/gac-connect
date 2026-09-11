"""Endpoints, regions and protocol constants for the GAC international API."""
from __future__ import annotations

from typing import Final

# Main account API and the IoV vehicle gateway are two separate backends with
# two separate crypto profiles.
IOV_PREFIX: Final = "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway"

# region -> (main API host, IoV gateway host). AU/NZ are the supported regions;
# the others are best-effort.
REGIONS: Final[dict[str, dict[str, str]]] = {
    "AU": {"main": "nl-app-api.gac-international.com", "iov": "eu-iov-sdk-access.gac-international.com",
           "tel": "+61", "tz": "Australia/Brisbane", "supported": True},
    "NZ": {"main": "nl-app-api.gac-international.com", "iov": "eu-iov-sdk-access.gac-international.com",
           "tel": "+64", "tz": "Pacific/Auckland", "supported": True},
    "GB": {"main": "nl-app-api.gac-international.com", "iov": "eu-iov-sdk-access.gac-international.com",
           "tel": "+44", "tz": "Europe/London", "supported": False},
    "SG": {"main": "sg-app-api.gac-international.com", "iov": "eu-iov-sdk-access.gac-international.com",
           "tel": "+65", "tz": "Asia/Singapore", "supported": False},
    "AE": {"main": "sa-app-api.gac-international.com", "iov": "eu-iov-sdk-access.gac-international.com",
           "tel": "+971", "tz": "Asia/Dubai", "supported": False},
}
DEFAULT_REGION: Final = "AU"

# Header identity the gateway expects (Android client profile).
APP_VERSION: Final = "2.0.25"
MAIN_APP_ID: Final = "app-android"
IOV_APP_ID: Final = "2"
IOV_VERSION: Final = "v1"
USER_AGENT: Final = "Dart/3.7 (dart:io)"

# SSO service that mints the IoV ticket from a main-API session.
SSO_SERVICE_ID: Final = "AIA-TSP"

# Captcha canvas geometry (anji-plus block puzzle).
PUZZLE_WIDTH: Final = 310
PUZZLE_HEIGHT: Final = 155
PUZZLE_PIECE_WIDTH: Final = 47
PUZZLE_MAX_X: Final = PUZZLE_WIDTH - PUZZLE_PIECE_WIDTH  # slider travel, 0..263

# Timeouts (seconds).
HTTP_TIMEOUT: Final = 20.0
# The gateway refreshes vehicle status about every 30 s; polling faster is wasteful.
MIN_POLL_INTERVAL: Final = 30.0
# Hard floor between ANY two gateway requests, serialized in the client. Normal
# polling is minutes apart so this never bites; it only caps bursts/misuse.
MIN_REQUEST_GAP: Final = 1.0
# Rolling budgets per client, as (window seconds, most requests in that window).
# Every gateway request counts, whatever triggered it (polls, refreshes, sign-in,
# push-channel details, commands). Past a limit the client refuses locally with
# RateLimitedError and sends nothing until the window frees up. Normal use (a poll
# every few minutes plus the odd command) stays far below all of them.
REQUEST_BUDGETS: Final = ((60, 20), (3600, 240), (86400, 3000))
# Vehicle commands reach the car itself, so they get their own, tighter budgets.
COMMAND_BUDGETS: Final = ((60, 6), (3600, 60))
# After the gateway answers 429, nothing is sent for this long, or for its
# Retry-After if that is longer (capped at MAX_RATE_LIMIT_COOLDOWN, one day).
RATE_LIMIT_COOLDOWN: Final = 60.0
MAX_RATE_LIMIT_COOLDOWN: Final = 86400.0
# If a saved request-limit record is corrupt, any pause in it is unknown: pause as
# long as the longest pause the service could have asked for.
RECOVERY_PAUSE: Final = MAX_RATE_LIMIT_COOLDOWN

# Gateway result codes.
CODE_OK: Final = "0000"
CODE_COMMAND_ACCEPTED: Final = 13001      # async command queued ("ongoing") — success
CODE_COMMAND_MALFORMED: Final = 13101
ERR_TOKEN_INVALID: Final = "SDK.GATE.0007"
ERR_VIN_HEADER: Final = "SDK.GATE.0017"
ERR_REPEAT_REQUEST: Final = "SDK.GATE.0018"
ERR_REFRESH_SPENT: Final = "ACCOUNT.0015"
ERR_CAPTCHA_WRONG: Final = 6111           # slider position wrong
ERR_CAPTCHA_TICKET: Final = 6110          # bad/expired captchaTicket
ERR_SERVICE_ID: Final = 1001005001        # unsupported serviceId
