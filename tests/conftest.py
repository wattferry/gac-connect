import math

import pytest


@pytest.fixture(autouse=True)
def _relaxed_default_limiter():
    """Every client also passes the process-wide limiter; in tests it is fresh and
    unrestricted, and each test uses its own limiter for the limits it checks."""
    from gac_connect.limits import DEFAULT_LIMITER as d
    saved = (d.min_gap, d.request_budgets, d.command_budgets, d.state_path)
    d.min_gap, d.request_budgets, d.command_budgets, d.state_path = 0.0, ((60, 10**9),), ((60, 10**9),), None
    d._sent.clear(); d._commands.clear(); d._blocked_until = 0.0; d._last_done = -math.inf
    yield
    d.min_gap, d.request_budgets, d.command_budgets, d.state_path = saved
    d._sent.clear(); d._commands.clear(); d._blocked_until = 0.0; d._last_done = -math.inf
