"""`utils/retry.py` had no call sites and no tests.

It was a generic decorator sitting next to a hand-rolled loop in
``llm/ollama.py`` that could not use it: the decorator selects what to retry by
exception *type*, and the rule that mattered -- retry ``URLError`` when its
``.reason`` is a timeout, raise it when the connection was refused -- lives on
the instance, not the class. Adding a predicate is what let the loop move here.

Now that it carries a real caller, it needs to be held to one.
"""

from __future__ import annotations

import urllib.error

import pytest

from reel_scout.utils.retry import retry, retry_call


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Record the sleeps instead of taking them, so backoff is assertable."""
    slept = []
    monkeypatch.setattr("reel_scout.utils.retry.time.sleep", slept.append)
    return slept


def _flaky(failures, exc=None, result="ok"):
    """Raises `failures` times, then returns `result`. Counts its calls."""
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] <= failures:
            raise exc or RuntimeError("not yet")
        return result

    fn.calls = state
    return fn


def test_a_call_that_works_is_not_retried_or_slept_on(no_sleeping):
    fn = _flaky(0)
    assert retry_call(fn) == "ok"
    assert fn.calls["n"] == 1
    assert no_sleeping == []


def test_it_keeps_trying_until_the_call_lands(no_sleeping):
    fn = _flaky(2)
    assert retry_call(fn, max_attempts=3, delay=1.0) == "ok"
    assert fn.calls["n"] == 3


def test_the_last_attempt_raises_instead_of_returning_something_empty(no_sleeping):
    fn = _flaky(99)
    with pytest.raises(RuntimeError):
        retry_call(fn, max_attempts=2, delay=1.0)
    assert fn.calls["n"] == 2
    # Two attempts means one gap between them, not two: announcing or sleeping
    # after the final failure would be waiting for something that is not coming.
    assert no_sleeping == [1.0]


def test_the_predicate_can_refuse_an_exception_its_own_class_would_accept():
    """The whole reason this module grew a predicate.

    Both of these are URLError. One is worth waiting out and one will fail the
    same way on attempt three, so no tuple of types can separate them.
    """
    import socket

    timed_out = urllib.error.URLError(socket.timeout("timed out"))
    refused = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    def is_timeout(exc):
        return isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError))

    slow = _flaky(1, exc=timed_out)
    assert retry_call(slow, max_attempts=3, delay=0, should_retry=is_timeout) == "ok"
    assert slow.calls["n"] == 2

    dead = _flaky(99, exc=refused)
    with pytest.raises(urllib.error.URLError):
        retry_call(dead, max_attempts=3, delay=0, should_retry=is_timeout)
    assert dead.calls["n"] == 1, "a refused connection must not be waited out"


def test_the_exception_tuple_still_filters_before_the_predicate():
    def _boom():
        raise KeyError("not in the tuple")

    called = []
    with pytest.raises(KeyError):
        retry_call(_boom, max_attempts=3, exceptions=(ValueError,),
                   should_retry=lambda e: called.append(e) or True)
    assert called == [], "the predicate must not even see what the tuple excluded"


def test_the_wait_doubles_and_is_announced_before_each_sleep(no_sleeping):
    seen = []
    fn = _flaky(3)
    retry_call(fn, max_attempts=4, delay=5.0, backoff=2.0,
               on_retry=lambda exc, attempt, wait: seen.append((attempt, wait)))
    assert no_sleeping == [5.0, 10.0, 20.0]
    # attempt is 1-based: "attempt 1 of 4 failed" is what a human reads.
    assert seen == [(1, 5.0), (2, 10.0), (3, 20.0)]


def test_a_retry_nobody_can_see_is_indistinguishable_from_a_slow_call(no_sleeping):
    # Not a style point: the incident this mechanism exists for looked exactly
    # like "the machine was slow that night". Without the announcement there is
    # nothing in the output that says otherwise.
    seen = []
    retry_call(_flaky(1), max_attempts=2, delay=1.0,
               on_retry=lambda exc, attempt, wait: seen.append(exc))
    assert len(seen) == 1


def test_zero_attempts_still_calls_once_instead_of_raising_none(no_sleeping):
    # The original loop did `for attempt in range(max_attempts)` and then
    # `raise last_exc`, so max_attempts=0 raised TypeError from `raise None`
    # -- an error about the retry helper, replacing the error from the work.
    fn = _flaky(0)
    assert retry_call(fn, max_attempts=0) == "ok"
    assert fn.calls["n"] == 1


def test_the_decorator_still_works_and_shares_the_one_loop(no_sleeping):
    state = {"n": 0}

    @retry(max_attempts=3, delay=1.0)
    def flaky(x):
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("not yet")
        return x * 2

    assert flaky(21) == 42
    assert state["n"] == 3
