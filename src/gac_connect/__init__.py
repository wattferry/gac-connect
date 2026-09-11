"""GAC Connect — an unofficial client for GAC / Aion international connected-car accounts.

Not affiliated with, endorsed by, or supported by GAC. For use with a vehicle you
own, on your own account.
"""
from __future__ import annotations

from .client import command_session_id
from .errors import (
    AuthError,
    AuthExpiredError,
    CaptchaError,
    CommandError,
    GacError,
    LoginError,
    PinRequiredError,
    RateLimitedError,
    RegionError,
)
from .push import BrokerInfo, PushClient, PushResult, interpret

__version__ = "0.2.0b7"

__all__ = [
    "AuthError",
    "BrokerInfo",
    "PushClient",
    "PushResult",
    "interpret",
    "AuthExpiredError",
    "CaptchaError",
    "CommandError",
    "GacError",
    "LoginError",
    "PinRequiredError",
    "RateLimitedError",
    "RegionError",
    "__version__",
    "command_session_id",
]
