"""Retrying the calls worth retrying, and only those.

This module sat here with a generic decorator and zero call sites while
``llm/ollama.py`` grew a hand-rolled loop beside it. The duplication was not
laziness on either side: the decorator selects what to retry by *exception
type*, and the one caller that needed retries could not express its rule that
way. ``URLError`` must be retried when its ``.reason`` is a timeout and raised
immediately when it is a refused connection -- same class, opposite answers.
A type tuple wide enough to catch the first also catches a 404, and retrying a
404 just delays the same error by ``timeout x attempts``.

So the predicate is the fix: ``should_retry`` decides per exception instance,
which is the granularity the real rule lives at. ``retry_call`` carries the
semantics and the decorator delegates to it, so there is one loop rather than
two that drift.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Optional, Tuple, Type


def retry_call(
    func: Callable[[], Any],
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[[BaseException, int, float], None]] = None,
) -> Any:
    """Call ``func()``, retrying with exponential backoff. Returns its result.

    ``should_retry`` is consulted per raised instance; returning False re-raises
    on the spot. It runs *after* the ``exceptions`` filter, so the tuple stays a
    cheap first pass and the predicate handles the cases a type cannot express.

    ``on_retry(exc, attempt, wait)`` is called before each sleep -- ``attempt``
    is 1-based. It exists because a retry that no one can see is indistinguishable
    from a call that was simply slow, and "the box was busy" is precisely the
    diagnosis this whole mechanism is here to make visible.

    The last attempt never sleeps and never calls ``on_retry``: there is nothing
    left to wait for, and announcing a retry that will not happen is a lie.
    """
    attempts = max(1, max_attempts)
    wait = delay
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except exceptions as exc:
            last = attempt == attempts
            if last or (should_retry is not None and not should_retry(exc)):
                raise
            if on_retry is not None:
                on_retry(exc, attempt, wait)
            time.sleep(wait)
            wait *= backoff
    # Unreachable: the loop runs at least once and every path returns or raises.
    # Kept loud rather than falling out with an implicit None, which would hand
    # the caller a successful-looking empty result -- the shape this package
    # keeps getting bitten by.
    raise AssertionError("unreachable")  # pragma: no cover


def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[[BaseException, int, float], None]] = None,
) -> Callable:
    """Decorator form of :func:`retry_call`.

    Use this when the settings are fixed at import time. When they come from
    ``config`` -- which the tests monkeypatch and an operator sets per run --
    call :func:`retry_call` directly instead: a decorator freezes its arguments
    when the module is imported, which would make those settings unreachable.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return retry_call(
                lambda: func(*args, **kwargs),
                max_attempts=max_attempts, delay=delay, backoff=backoff,
                exceptions=exceptions, should_retry=should_retry, on_retry=on_retry,
            )
        return wrapper
    return decorator
