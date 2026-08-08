from __future__ import annotations

from qgis_agent_mcp.bridge_scheduler import BridgeScheduler


def test_scheduler_prioritizes_control_and_preserves_fifo_per_queue():
    scheduler = BridgeScheduler({"project.action"}, max_pending=10, control_reserve=2)
    socket = object()
    assert scheduler.enqueue(socket, {"id": 1, "method": "project.action"}, now=1)
    assert scheduler.enqueue(socket, {"id": 2, "method": "session.snapshot"}, now=2)
    assert scheduler.enqueue(socket, {"id": 3, "method": "operation.control"}, now=3)
    assert scheduler.enqueue(socket, {"id": 4, "method": "session.snapshot"}, now=4)

    assert [scheduler.pop_next().request["id"] for _ in range(4)] == [3, 2, 4, 1]


def test_scheduler_reserves_capacity_for_control_requests():
    scheduler = BridgeScheduler(set(), max_pending=4, control_reserve=1)
    socket = object()
    for request_id in range(3):
        assert scheduler.enqueue(
            socket, {"id": request_id, "method": "session.snapshot"}
        )
    assert not scheduler.enqueue(socket, {"id": 4, "method": "session.snapshot"})
    assert scheduler.enqueue(socket, {"id": 5, "method": "operation.control"})
    assert scheduler.snapshot()["rejected"] == 1


def test_scheduler_discards_disconnected_clients_and_tracks_deadlines():
    scheduler = BridgeScheduler(set(), deadline_seconds=2)
    first = object()
    second = object()
    scheduler.enqueue(first, {"id": 1, "method": "session.snapshot"}, now=10)
    scheduler.enqueue(second, {"id": 2, "method": "session.snapshot"}, now=10)

    assert scheduler.discard_socket(first) == 1
    item = scheduler.pop_next()
    assert item.socket is second
    assert item.expired(now=12)
    scheduler.record(2000, expired=True)
    assert scheduler.snapshot()["expired"] == 1


def test_scheduler_prevents_mutation_starvation_and_cancels_queued_requests():
    scheduler = BridgeScheduler({"project.action"})
    socket = object()
    assert scheduler.enqueue(socket, {"id": 99, "method": "project.action"})
    for request_id in range(10):
        assert scheduler.enqueue(
            socket, {"id": request_id, "method": "session.snapshot"}
        )
    assert [scheduler.pop_next().request["id"] for _ in range(4)] == [0, 1, 2, 99]

    assert scheduler.cancel_request(socket, 5) is True
    assert scheduler.snapshot()["cancelled"] == 1
    assert scheduler.cancel_request(socket, 5) is False
