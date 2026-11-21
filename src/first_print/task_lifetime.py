"""Wait for work whose cleanup must finish before its owner goes away."""

import asyncio


async def finish_owned[T](task: asyncio.Task[T]) -> T:
    """Defer caller cancellation until this owned task settles; never cancel it again.

    The caller retains the task and remains responsible for its lifetime. Task failures
    propagate normally, and a cancelled child still raises CancelledError.
    """
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancelled = error
        else:
            if cancelled is not None:
                raise cancelled
            return result
