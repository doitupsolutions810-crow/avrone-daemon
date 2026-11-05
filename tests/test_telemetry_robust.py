"""Round 40: telemetry / system map / mermaid against hostile and broken run logs."""
import json
import os
import time
from pathlib import Path

import pytest

from avrone import telemetry as tm
from tests.test_telemetry_edges import _core, ev

NOW = time.time


def write_lines(core, name, lines, mtime=None):
    p = Path(core.codex.log_dir) / f"{name}.jsonl"
    p.write_bytes(b"\n".join(l if isinstance(l, bytes) else l.encode() for l in lines) + b"\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def J(e):
    return json.dumps(e)


# ---------------------------------------------------------------- one bad line never 500s the HUD
@pytest.mark.parametrize("line", [
    "[1, 2, 3]", "\"just a string\"", "42", "null", "true", "{not json",
    "1" * 5000, "[" * 100000, "{\"ts\": " + "9" * 5000 + "}",
])
def test_non_object_or_unparseable_lines_skipped(tmp_path, line):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start", mode="chat")), line, J(ev("run.done"))])
    t = tm.telemetry(core, 3600)
    assert t["runs"]["total"] == 1 and t["runs"]["completed"] == 1
    tm.system_map(core, 3600)


def test_bad_utf8_does_not_crash(tmp_path):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start", mode="chat")), b"\xff\xfe\x80 garbage", J(ev("run.done"))])
    assert tm.telemetry(core, 3600)["runs"]["completed"] == 1


@pytest.mark.parametrize("ts", ["yesterday", None, True, [], {}, "NaN"])
def test_junk_ts_reads_as_zero_and_is_out_of_window(tmp_path, ts):
    core = _core(tmp_path)
    e = ev("run.start", rid="bad", mode="chat")
    e["ts"] = ts
    write_lines(core, "r1", [J(e), J(ev("run.start", rid="ok", mode="chat"))])
    t = tm.telemetry(core, 3600)
    assert t["runs"]["total"] == 1


def test_nan_and_inf_ts_lines(tmp_path):
    core = _core(tmp_path)
    write_lines(core, "r1", ['{"run_id": "a", "kind": "run.start", "ts": NaN, "data": {}}',
                             '{"run_id": "b", "kind": "run.start", "ts": Infinity, "data": {}}',
                             J(ev("run.start", rid="c"))])
    assert tm.telemetry(core, 3600)["runs"]["total"] == 1


@pytest.mark.parametrize("rid", [["x"], {"a": 1}, 1.5, True, None])
def test_unhashable_or_odd_run_ids_skipped(tmp_path, rid):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start", rid=rid, mode="chat")), J(ev("run.done", rid=rid)),
                             J(ev("run.start", rid="ok", mode="chat"))])
    t = tm.telemetry(core, 3600)
    assert t["runs"]["total"] == 1 and t["recent_runs"][0]["run_id"] == "ok"


def test_int_run_id_kept_as_string(tmp_path):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start", rid=7, mode="chat")), J(ev("run.done", rid=7))])
    t = tm.telemetry(core, 3600)
    assert t["recent_runs"][0]["run_id"] == "7" and t["runs"]["completed"] == 1


@pytest.mark.parametrize("data", [[1, 2], "str", 5, None])
def test_non_dict_data(tmp_path, data):
    core = _core(tmp_path)
    e = ev("tool.call", agent="W-1")
    e["data"] = data
    write_lines(core, "r1", [J(ev("run.start", mode="chat")), J(e)])
    t = tm.telemetry(core, 3600)
    assert t["runs"]["total"] == 1
    tm.system_map(core, 3600)


@pytest.mark.parametrize("mode", [["a"], {"x": 1}, 3])
def test_unhashable_mode(tmp_path, mode):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start", mode=mode))])
    assert tm.telemetry(core, 3600)["runs"]["by_mode"] == {None: 1}


@pytest.mark.parametrize("agent,kind", [(["W"], "tool.call"), (5, "tool.call"), (None, "agent.spawn"),
                                        ("W-1", ["run.start"]), ("W-1", 7)])
def test_odd_agent_and_kind(tmp_path, agent, kind):
    core = _core(tmp_path)
    e = ev("tool.call", agent="W-1", tool="shell")
    e["agent"], e["kind"] = agent, kind
    write_lines(core, "r1", [J(e)])
    tm.telemetry(core, 3600)
    tm.system_map(core, 3600)


@pytest.mark.parametrize("parent", [["x"], 5, {"a": 1}, True, "", None, "Lead-3"])
def test_spawn_parent_types(tmp_path, parent):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("agent.spawn", role="Coder", parent=parent))])
    g = tm.system_map(core, 3600)
    assert any(n["id"] == "role:Coder" for n in g["nodes"])


def test_negative_duration_clamped(tmp_path):
    core = _core(tmp_path)
    t0 = NOW()
    write_lines(core, "r1", [J(ev("run.start", ts=t0)), J(ev("run.done", ts=t0 - 50))])
    t = tm.telemetry(core, 3600)
    assert t["runs"]["completed"] == 1 and t["runs"]["p50_s"] == 0.0


def test_done_at_ts_zero_counts_completed(tmp_path):
    # end == start == small ts used to read falsy and count as running; window covers it via max
    core = _core(tmp_path)
    t0 = NOW()
    write_lines(core, "r1", [J(ev("run.start", ts=t0)), J(ev("run.done", ts=t0))])
    assert tm.telemetry(core, 3600)["runs"]["running"] == 0


# ---------------------------------------------------------------- files
def test_file_deleted_between_glob_and_stat(tmp_path, monkeypatch):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start", mode="chat"))])
    ghost = Path(core.codex.log_dir) / "ghost.jsonl"
    real_glob = Path.glob

    def glob(self, pat):
        yield from real_glob(self, pat)
        yield ghost
    monkeypatch.setattr(Path, "glob", glob)
    assert tm.telemetry(core, 3600)["runs"]["total"] == 1


def test_unreadable_file_skipped(tmp_path, monkeypatch):
    core = _core(tmp_path)
    write_lines(core, "a", [J(ev("run.start", rid="a"))])
    write_lines(core, "b", [J(ev("run.start", rid="b"))])
    real = tm._read_tail
    monkeypatch.setattr(tm, "_read_tail", lambda p, n: None if p.name == "a.jsonl" else real(p, n))
    assert tm.telemetry(core, 3600)["runs"]["total"] == 1


def test_read_tail_permission_error(monkeypatch, tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("{}\n")
    real_open = open

    def denied(path, *args, **kwargs):
        if Path(path) == p:
            raise PermissionError("fixture denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    assert tm._read_tail(p, 1000) is None


def test_log_dir_missing(tmp_path):
    core = _core(tmp_path)
    core.codex.log_dir = tmp_path / "nope"
    assert tm.telemetry(core, 3600)["runs"]["total"] == 0


def test_log_dir_is_a_file(tmp_path):
    core = _core(tmp_path)
    f = tmp_path / "file"
    f.write_text("x")
    core.codex.log_dir = f
    assert tm.telemetry(core, 3600)["runs"]["total"] == 0


def test_huge_log_read_from_tail_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_TELEMETRY_MAX_FILE_BYTES", str(64 << 10))
    core = _core(tmp_path)
    filler = J(ev("tool.call", agent="W-1", tool="shell", pad="x" * 500))
    lines = [J(ev("run.start", rid="early"))] + [filler] * 400 + [J(ev("run.start", rid="late"))]
    write_lines(core, "big", lines)
    t = tm.telemetry(core, 3600)
    ids = {r["run_id"] for r in t["recent_runs"]}
    assert "late" in ids and "early" not in ids


def test_read_tail_drops_partial_first_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"a": 1}\n' + b'{"b": 2}\n')
    out = tm._read_tail(p, 12)
    assert out == '{"b": 2}\n'


def test_read_tail_small_file_whole(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"a": 1}\n')
    assert tm._read_tail(p, 1 << 20) == '{"a": 1}\n'


def test_read_tail_single_huge_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b"x" * 5000)
    assert tm._read_tail(p, 100) == ""


@pytest.mark.parametrize("v,want", [("", 8 << 20), ("junk", 8 << 20), ("1", 64 << 10),
                                    (str(10 << 30), 256 << 20), (str(1 << 20), 1 << 20)])
def test_max_file_bytes_env(monkeypatch, v, want):
    monkeypatch.setenv("AVRONE_TELEMETRY_MAX_FILE_BYTES", v)
    assert tm.max_file_bytes() == want


def test_max_files_odd_values(tmp_path):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start"))])
    assert len(list(tm._iter_events(core.codex.log_dir, 0, max_files=0))) == 1


def test_iter_since_nan(tmp_path):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start"))])
    assert len(list(tm._iter_events(core.codex.log_dir, float("nan")))) == 1


# ---------------------------------------------------------------- window
@pytest.mark.parametrize("w,want", [(float("nan"), 86400.0), (float("inf"), 86400.0), (0, 86400.0), (-5, 86400.0),
                                    ("24", 86400.0), (None, 86400.0), (True, 86400.0), (60, 60.0),
                                    (10 ** 12, 366 * 86400.0)])
def test_clamp_window(w, want):
    assert tm.clamp_window(w) == want


def test_nan_window_still_shows_runs(tmp_path):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("run.start", mode="chat"))])
    t = tm.telemetry(core, float("nan"))
    assert t["window_s"] == 86400.0 and t["runs"]["total"] == 1
    assert any(n["id"].startswith("tool:") for n in tm.system_map(core, float("nan"))["nodes"])


def test_api_hours_nan(tmp_path, monkeypatch):
    from tests.test_telemetry import client as _c  # noqa: F401  (fixture module import check)
    assert tm.clamp_window(float("nan") * 3600) == 86400.0


# ---------------------------------------------------------------- active agents
@pytest.mark.parametrize("active", [{"W-1": "working"}, {"W-1": None, "W-2": {"status": "working"}}, None, [], 5])
def test_active_now_odd_shapes(tmp_path, active):
    core = _core(tmp_path, active=active)
    out = tm.telemetry(core, 3600)["agents"]["active_now"]
    assert out in ([], ["W-2"])


# ---------------------------------------------------------------- mermaid
def test_mermaid_ids_do_not_collide():
    g = {"nodes": [{"id": "a-b", "label": "A"}, {"id": "a_b", "label": "B"}],
         "edges": [{"from": "a-b", "to": "a_b", "weight": 1}]}
    m = tm.to_mermaid(g)
    lines = m.splitlines()
    ids = [l.split("[")[0].strip() for l in lines[1:3]]
    assert ids[0] != ids[1]
    assert f"{ids[0]} -->|1| {ids[1]}" in m


@pytest.mark.parametrize("bad", ['x"] ; click n_core call alert(1) ; a["', "line1\nclick n_core href \"javascript:x\"",
                                 "a|b", "a`b", "a<script>b", "a{b}c", "a]b["])
def test_mermaid_label_injection_escaped(bad):
    m = tm.to_mermaid({"nodes": [{"id": "core", "label": bad}], "edges": []})
    body = m.splitlines()
    assert len(body) == 2  # no newline smuggled a statement in
    inner = body[1].split('["', 1)[1].rsplit('"]', 1)[0]
    for ch in '"[]|`<>{}\n':
        assert ch not in inner


def test_mermaid_label_capped():
    m = tm.to_mermaid({"nodes": [{"id": "x", "label": "y" * 5000}], "edges": []})
    assert len(m.splitlines()[1]) < 200


def test_mermaid_non_ascii_ids_safe():
    m = tm.to_mermaid({"nodes": [{"id": "rôle:δ", "label": "δ"}, {"id": "rôle:γ", "label": "γ"}], "edges": []})
    ids = [l.split("[")[0].strip() for l in m.splitlines()[1:]]
    assert all(i.isascii() for i in ids) and ids[0] != ids[1]


def test_mermaid_tolerates_junk_graph():
    g = {"nodes": [None, {"label": "no id"}, {"id": 5}, {"id": "ok", "label": None}],
         "edges": [None, {"from": "ok"}, {"from": "ok", "to": "ok", "weight": "x"},
                   {"from": "ok", "to": 5, "weight": float("nan")}, {"from": "ok", "to": "ok", "weight": 2.5}]}
    m = tm.to_mermaid(g)
    assert m.startswith("flowchart LR") and "|2.5|" in m


def test_mermaid_min_weight_junk():
    g = {"nodes": [], "edges": [{"from": "a", "to": "b", "weight": 3}]}
    assert "-->|3|" in tm.to_mermaid(g, min_weight="x")
    assert "-->" not in tm.to_mermaid(g, min_weight=5)


def test_mermaid_empty_graph():
    assert tm.to_mermaid({}) == "flowchart LR"


def test_mermaid_stable_ids_per_call():
    g = {"nodes": [{"id": "core", "label": "C"}], "edges": [{"from": "core", "to": "core", "weight": 0}]}
    assert tm.to_mermaid(g) == tm.to_mermaid(g)
    assert "n_core" in tm.to_mermaid(g)


def test_full_map_with_hostile_tool_name_renders(tmp_path):
    core = _core(tmp_path)
    write_lines(core, "r1", [J(ev("tool.call", agent="W-1", tool='x"]\nclick n_core call evil()\n%%'))])
    m = tm.to_mermaid(tm.system_map(core, 3600))
    assert "\nclick" not in m and "call evil()" in m  # text survives, escaped inside a label
