"""capability-authority execute shapes. No socket, no local runner."""
from avrone.fusion_plane import NARROW_ACTION, classify_execute, plane_availability


def test_main_stub_is_not_execution():
    # main Executor.execute, port default 8090, no execution_status.
    body = {"status": "ok", "result": "executed", "receipt": {"outcome": "ok"}}
    got = classify_execute(body)
    assert got == {"executed": False, "reason": "unscoped_stub", "runner": "none"}


def test_not_implemented_stays_unauthorized_to_run():
    body = {
        "status": "authorized",
        "result": "not_executed",
        "execution_status": "NOT_IMPLEMENTED",
        "idempotent_replay": False,
    }
    got = classify_execute(body)
    assert got["executed"] is False
    assert got["reason"] == "not_implemented"
    assert got["runner"] == "none"


def test_only_succeeded_counts_and_plane_is_not_called():
    body = {"status": "executed", "execution_status": "SUCCEEDED", "result": {"sha256": "abc"}}
    assert classify_execute(body)["executed"] is True
    assert classify_execute(body)["runner"] == "plane"
    assert classify_execute([])["reason"] == "malformed"
    assert classify_execute({"status": "error", "error": "verify failed"})["executed"] is False
    record = plane_availability()
    assert record["daemon_calls_execute"] is False
    assert record["pr17_merged"] is False
    assert record["main_port"] == 8090
    assert record["pr17_port"] == 8095
    assert record["narrow_action"] == NARROW_ACTION
