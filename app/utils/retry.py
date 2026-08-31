# savoury-spud-backend/app/utils/retry.py
#
# Exponential backoff decorator for any async function.
# Used by whatsapp.py and paystack.py for transient network errors.
#
# Usage:
#   @with_retry(max_attempts=3, base_delay=1.0)
#   async def send_message(...):
#       ...

import asyncio
import functools
from typing import Callable, Type
from app.utils.logger import log


def with_retry(
    max_attempts: int = 3,
    base_delay:   float = 1.0,       # seconds; doubles each attempt
    exceptions:   tuple[Type[Exception], ...] = (Exception,),
):
    """
    Decorator factory. Wraps an async function with retry logic.

    On each failure:
      - Waits base_delay * (2 ** attempt) seconds
      - Attempt 1: 1s, Attempt 2: 2s, Attempt 3: 4s

    After max_attempts, re-raises the last exception.
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    delay = base_delay * (2 ** attempt)

                    await log.warn(
                        "RETRY_ATTEMPT",
                        metadata={
                            "function":  func.__name__,
                            "attempt":   attempt + 1,
                            "max":       max_attempts,
                            "delay_s":   delay,
                            "error":     str(e),
                        },
                    )

                    if attempt < max_attempts - 1:
                        await asyncio.sleep(delay)

            raise last_exc  # type: ignore

        return wrapper
    return decorator
