import asyncio
import logging

import pytest

from telepath.user_client import DEFAULT_VOICE_QUEUE_MAXSIZE, ChannelReactionQueueWorker, VoiceQueueWorker


class _Event:
    def __init__(self, chat_id: int, message_id: int):
        self.chat_id = chat_id
        self.message = type("M", (), {"id": message_id})()


async def _drain(worker: VoiceQueueWorker, *, expected: int, timeout: float = 1.0) -> None:
    """Wait until `expected` events have been processed (best-effort)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while worker.processed_count < expected:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"timed out waiting for {expected} processed events; got {worker.processed_count}"
            )
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------


def test_worker_rejects_non_positive_maxsize():
    async def noop(event):
        return None

    with pytest.raises(ValueError, match="maxsize must be a positive integer"):
        VoiceQueueWorker(noop, maxsize=0)
    with pytest.raises(ValueError, match="maxsize must be a positive integer"):
        VoiceQueueWorker(noop, maxsize=-3)


def test_worker_exposes_default_maxsize():
    async def noop(event):
        return None

    worker = VoiceQueueWorker(noop)
    assert worker.maxsize == DEFAULT_VOICE_QUEUE_MAXSIZE
    assert worker.qsize() == 0


# ---------------------------------------------------------------------------
# ordering: events handed to the consumer in FIFO order, never concurrently
# ---------------------------------------------------------------------------


async def test_worker_processes_events_sequentially_in_fifo_order():
    processed: list[int] = []
    concurrency_peak = 0
    inflight = 0

    async def handler(event):
        nonlocal inflight, concurrency_peak
        inflight += 1
        concurrency_peak = max(concurrency_peak, inflight)
        # Yield to the loop so any "parallel" handler would have a chance to start.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        processed.append(event.message.id)
        inflight -= 1

    worker = VoiceQueueWorker(handler=handler)
    consumer = asyncio.create_task(worker.run())

    for i in range(10):
        assert worker.submit(_Event(chat_id=100, message_id=i)) is True

    await _drain(worker, expected=10)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert processed == list(range(10)), "events must be handled in submission order"
    assert concurrency_peak == 1, "consumer must never run two handlers in parallel"
    assert worker.processed_count == 10
    assert worker.dropped_count == 0


# ---------------------------------------------------------------------------
# exception resilience
# ---------------------------------------------------------------------------


async def test_worker_continues_after_handler_exception(caplog):
    seen: list[int] = []

    async def handler(event):
        seen.append(event.message.id)
        if event.message.id == 1:
            raise RuntimeError("boom")

    worker = VoiceQueueWorker(handler=handler)
    with caplog.at_level(logging.ERROR):
        consumer = asyncio.create_task(worker.run())

        for i in range(3):
            worker.submit(_Event(chat_id=100, message_id=i))

        await _drain(worker, expected=2)  # only 0 and 2 succeed; 1 raises

        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

    assert seen == [0, 1, 2], "all three events should reach the handler"
    assert worker.processed_count == 2, "failed event must not be counted as processed"
    assert "voice_consumer_handler_failed" in caplog.text
    assert "boom" in caplog.text


# ---------------------------------------------------------------------------
# backpressure: queue full → drop + log, never blocks producer
# ---------------------------------------------------------------------------


async def test_worker_drops_events_when_queue_is_full(caplog):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(event):
        started.set()
        await release.wait()

    worker = VoiceQueueWorker(handler=slow_handler, maxsize=2)
    consumer = asyncio.create_task(worker.run())

    # Fill the pipeline: 1 in-flight + 2 buffered = 3 accepted. The 4th and
    # 5th must be dropped without blocking.
    assert worker.submit(_Event(100, 0)) is True
    await started.wait()  # ensure the consumer pulled the first event
    assert worker.submit(_Event(100, 1)) is True
    assert worker.submit(_Event(100, 2)) is True

    with caplog.at_level(logging.WARNING):
        assert worker.submit(_Event(100, 3)) is False
        assert worker.submit(_Event(100, 4)) is False

    assert worker.dropped_count == 2
    assert "voice_dropped_queue_full" in caplog.text

    release.set()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer


async def test_worker_submit_is_synchronous_and_non_blocking():
    """Even if no consumer task is running, submit() must not block."""

    async def handler(event):
        return None

    worker = VoiceQueueWorker(handler=handler, maxsize=3)
    assert worker.submit(_Event(1, 1)) is True
    assert worker.submit(_Event(1, 2)) is True
    assert worker.submit(_Event(1, 3)) is True
    # 4th overflows without a consumer running; must return False instantly.
    assert worker.submit(_Event(1, 4)) is False
    assert worker.qsize() == 3
    assert worker.dropped_count == 1


async def test_channel_reaction_worker_reads_delay_range_for_each_event():
    events = []
    sleeps = []
    ranges = [(1, 1), (5, 5)]

    async def handler(event):
        events.append(event.message.id)

    async def sleep(delay):
        sleeps.append(delay)

    worker = ChannelReactionQueueWorker(
        handler=handler,
        delay_range_provider=lambda: ranges.pop(0),
        sleep=sleep,
    )
    consumer = asyncio.create_task(worker.run())

    assert worker.submit(_Event(100, 1))
    assert worker.submit(_Event(100, 2))
    await _drain(worker, expected=2)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert events == [1, 2]
    assert sleeps == [1, 5]


async def test_channel_reaction_worker_applies_delays_concurrently():
    events = []
    sleeps = []
    both_delays_started = asyncio.Event()
    release_delays = asyncio.Event()
    ranges = [(10, 10), (20, 20)]

    async def handler(event):
        events.append(event.message.id)

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            both_delays_started.set()
        await release_delays.wait()

    worker = ChannelReactionQueueWorker(
        handler=handler,
        delay_range_provider=lambda: ranges.pop(0),
        sleep=sleep,
    )
    consumer = asyncio.create_task(worker.run())

    assert worker.submit(_Event(100, 1))
    assert worker.submit(_Event(100, 2))
    await asyncio.wait_for(both_delays_started.wait(), timeout=1.0)
    assert events == []

    release_delays.set()
    await _drain(worker, expected=2)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert events == [1, 2]
    assert sleeps == [10, 20]


async def test_channel_reaction_worker_can_apply_post_dispatch_cooldown():
    events = []
    sleeps = []

    async def handler(event):
        events.append(event.message.id)

    async def sleep(delay):
        sleeps.append(delay)

    worker = ChannelReactionQueueWorker(
        handler=handler,
        delay_range_provider=lambda: (1, 1),
        post_dispatch_delay_range_seconds=(8, 8),
        sleep=sleep,
    )
    consumer = asyncio.create_task(worker.run())

    assert worker.submit(_Event(100, 1))
    await _drain(worker, expected=1)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert events == [1]
    assert sleeps == [1, 8]


async def test_channel_reaction_worker_limits_pending_delayed_events(caplog):
    delay_started = asyncio.Event()
    release_delay = asyncio.Event()

    async def handler(event):
        return None

    async def sleep(delay):
        delay_started.set()
        await release_delay.wait()

    worker = ChannelReactionQueueWorker(handler=handler, maxsize=1, sleep=sleep)
    consumer = asyncio.create_task(worker.run())

    assert worker.submit(_Event(100, 1))
    await delay_started.wait()
    with caplog.at_level(logging.WARNING):
        assert worker.submit(_Event(100, 2)) is False

    release_delay.set()
    await _drain(worker, expected=1)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert worker.dropped_count == 1
    assert "channel_reaction_dropped_queue_full" in caplog.text


async def test_channel_reaction_worker_skips_duplicate_posts(caplog):
    events = []

    async def handler(event):
        events.append((event.chat_id, event.message.id))

    worker = ChannelReactionQueueWorker(handler=handler, sleep=lambda delay: asyncio.sleep(0))
    consumer = asyncio.create_task(worker.run())

    assert worker.submit(_Event(100, 1)) is True
    with caplog.at_level(logging.INFO):
        assert worker.submit(_Event(100, 1)) is False
    await _drain(worker, expected=1)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert events == [(100, 1)]
    assert worker.processed_count == 1
    assert "channel_reaction_duplicate_skipped" in caplog.text


# ---------------------------------------------------------------------------
# graceful shutdown
# ---------------------------------------------------------------------------


async def test_worker_run_propagates_cancellation():
    async def handler(event):
        return None

    worker = VoiceQueueWorker(handler=handler)
    consumer = asyncio.create_task(worker.run())
    await asyncio.sleep(0)  # let the consumer enter its idle wait

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
