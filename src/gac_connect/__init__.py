"""GAC Connect — an unofficial client for GAC / Aion international connected-car accounts.

Not affiliated with, endorsed by, or supported by GAC. For use with a vehicle you
own, on your own account.
"""
from __future__ import annotations

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

__version__ = "0.2.0b4"

__all__ = [
    "AuthError",
    "AuthExpiredError",
    "CaptchaError",
    "CommandError",
    "GacError",
    "LoginError",
    "PinRequiredError",
    "RateLimitedError",
    "RegionError",
    "__version__",
]
