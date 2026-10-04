"""Rate-limit helpers (no network, no real sleeping)."""
from types import SimpleNamespace

from app.llm.rate_limit import MAX_RETRY_WAIT_S, TokenWindowLimiter, estimate_tokens, retry_after_seconds


class FakeClock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def test_under_budget_never_sleeps():
    c = FakeClock()
    lim = TokenWindowLimiter(1000, clock=c.clock, sleep=c.sleep)
    for _ in range(4):
        lim.wait(250)
    assert c.slept == []


def test_over_budget_waits_for_window_to_roll():
    c = FakeClock()
    lim = TokenWindowLimiter(1000, clock=c.clock, sleep=c.sleep)
    lim.wait(600)
    c.now = 10.0
    lim.wait(600)  # 1200 > 1000: must wait until the first event is 60s old
    assert c.now >= 60.0
    assert sum(c.slept) >= 50.0


def test_single_oversized_call_is_allowed():
    c = FakeClock()
    lim = TokenWindowLimiter(1000, clock=c.clock, sleep=c.sleep)
    lim.wait(5000)  # capped to the budget, does not deadlock
    assert c.slept == []


def test_estimate_tokens_scales_with_text():
    assert estimate_tokens("x" * 3500, output_allowance=0) == 1000


def _resp(headers=None, text=""):
    return SimpleNamespace(headers=headers or {}, text=text)


def test_retry_after_header_and_body_hints():
    assert retry_after_seconds(_resp({"retry-after": "12"}), 2.0) == 12.5
    assert retry_after_seconds(_resp(text="Please try again in 7.2s."), 2.0) == 7.7
    assert retry_after_seconds(_resp(text="try again in 800ms"), 2.0) == 2.0  # default is the floor
    assert retry_after_seconds(_resp(text='"retryDelay": "23s"'), 2.0) == 23.5


def test_retry_wait_is_capped_and_defaults_apply():
    assert retry_after_seconds(_resp({"retry-after": "9999"}), 2.0) == MAX_RETRY_WAIT_S
    assert retry_after_seconds(_resp(), 4.0) == 4.0
