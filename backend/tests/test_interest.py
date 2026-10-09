"""Client-interest ownership is independent from SSE transport."""

from __future__ import annotations

import time

import pytest

from nomusic.jobs import JobRegistry, JobState, JobStatus, WorkerAbandoned, _Execution


def _registry_with_job(**kwargs):
    registry = JobRegistry(None, None, **kwargs)
    status = JobStatus("job", state=JobState.PROCESSING)
    execution = _Execution("job", 1, status)
    with registry._lock:
        registry._jobs["job"] = status
        registry._executions["job"] = execution
    return registry


def test_releasing_one_client_does_not_cancel_another_or_depend_on_sse():
    registry = _registry_with_job(client_lease_seconds=30)
    first = registry.acquire_interest("job", "first")
    second = registry.acquire_interest("job", "second")
    assert first["leased"] and second["leased"]

    queue = registry.subscribe("job")
    registry.unsubscribe("job", queue)
    assert registry.release_interest("job", "first") is True
    assert "second" in registry._interests["job"]

    # The worker remains alive while the second tab holds its lease, even with
    # no status stream attached.
    registry._raise_if_abandoned("job", idle_timeout=0.001)

    assert registry.release_interest("job", "second") is True
    registry._last_disconnect_at["job"] = time.time() - 10
    with pytest.raises(WorkerAbandoned):
        registry._raise_if_abandoned("job", idle_timeout=0.001)


def test_expired_interest_is_reclaimed_and_then_allows_idle_abandon():
    registry = _registry_with_job(client_lease_seconds=1)
    registry.acquire_interest("job", "tab", lease_seconds=0.01)
    time.sleep(0.03)
    assert registry.expire_interests() == 1
    assert registry._interests == {}
    registry._last_disconnect_at["job"] = time.time() - 10
    with pytest.raises(WorkerAbandoned):
        registry._raise_if_abandoned("job", idle_timeout=0.001)


def test_sse_snapshot_queue_is_bounded_and_keeps_latest_state():
    registry = _registry_with_job(sse_queue_size=2)
    queue = registry.subscribe("job")
    registry._enqueue_snapshot(queue, {"state": "queued"})
    registry._enqueue_snapshot(queue, {"state": "processing"})
    registry._enqueue_snapshot(queue, {"state": "ready"})
    assert queue.qsize() == 2
    assert queue.get_nowait()["state"] == "processing"
    assert queue.get_nowait()["state"] == "ready"
