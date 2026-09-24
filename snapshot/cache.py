"""Bounded, process-local caching for successful network responses."""

from collections import OrderedDict
from copy import deepcopy
from functools import wraps
from threading import Lock
from time import monotonic


def ttl_cache(*, ttl: float, maxsize: int = 128, cacheable=lambda value: value is not None):
    """Cache successful values only; copies prevent callers mutating cached data.

    Locks protect metadata only: slow network work never blocks unrelated keys.
    Concurrent misses may fetch twice; the app already serializes capture jobs.
    """
    if ttl <= 0 or maxsize <= 0:
        raise ValueError("Cache TTL and capacity must be positive")

    def decorate(function):
        entries = OrderedDict()
        lock = Lock()

        @wraps(function)
        def wrapped(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = monotonic()
            with lock:
                cached = entries.get(key)
                if cached is not None:
                    expires, value = cached
                    if expires > now:
                        entries.move_to_end(key)
                        return deepcopy(value)
                    del entries[key]

            value = function(*args, **kwargs)
            if cacheable(value):
                stored = deepcopy(value)
                now = monotonic()
                with lock:
                    for expired in [k for k, (expires, _) in entries.items() if expires <= now]:
                        del entries[expired]
                    entries[key] = (now + ttl, stored)
                    entries.move_to_end(key)
                    while len(entries) > maxsize:
                        entries.popitem(last=False)
            return value

        def cache_clear():
            with lock:
                entries.clear()

        wrapped.cache_clear = cache_clear
        return wrapped

    return decorate
