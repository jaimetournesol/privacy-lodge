"""Bounded inference workers; cancellation never frees a still-running worker slot."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

class BusyError(RuntimeError):
    pass

class Workers:
    def __init__(self, workers=2, capacity=8):
        self.pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='voice-inference')
        self.slots=threading.BoundedSemaphore(capacity)

    async def run(self, fn, *args):
        if not self.slots.acquire(blocking=False):
            raise BusyError('Voice processing is busy; retry shortly.')
        try:
            future=self.pool.submit(fn,*args)
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(lambda _:self.slots.release())
        return await asyncio.wrap_future(future)

    def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)

WORKERS=Workers()
# Frame detection must stay responsive while transcription waits on a provider.
VAD_WORKERS=Workers(workers=2,capacity=8)
