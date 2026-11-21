"""Round 80: governor env/arg parsing (ASCII-only, bounded ttl/thermal, text on/off) and
lesson_forget/bridge_status edges (non-dict args, invisible reasons, unusable data_dir)."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace as NS

import pytest

from avrone import governor as g
from avrone import tools_lessonforget as lf

KEY = "l-" + "0" * 16


def fake(**kw):
    vals = {"load": 0.1, "mem_pct": 10.0, "disk_free_pct": 90.0, "temp_c": None, "thermal_limit_pct": None}
    vals.update(kw)
    return {k: (lambda v=v: v) for k, v in vals.items()}


# ------------------------------------------------------------------ governor
@pytest.mark.parametrize("raw", ["\uff12", "\u0663", "\u0e52", "\uff11\uff10"])
def test_as_int_refuses_non_ascii_digits(raw):
    assert g._as_int(raw, 7) == 7


@pytest.mark.parametrize("raw,want", [(" 3 ", 3), ("2.9", 2), ("5", 5)])
def test_as_int_plain_still_works(raw, want):
    assert g._as_int(raw, 7) == want


@pytest.mark.parametrize("raw,want", [("\uff13", 1), ("'3'", 3), (' "2" ', 2), ("", 1), ("4", 4)])
def test_env_min_parallel(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_MIN_PARALLEL", raw)
    assert g.Governor(8, readers=fake()).floor == want


@pytest.mark.parametrize("raw,want", [("\uff19\uff10", 85.0), ("-40", 85.0), ("0", 85.0), ("500", 85.0),
                                      ("'70'", 70.0), ("70", 70.0), ("150", 150.0)])
def test_env_thermal_max(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_THERMAL_MAX_C", raw)
    assert g.Governor(4, readers=fake()).thermal_max_c == want


@pytest.mark.parametrize("v,want", [(-40, 85.0), (0, 85.0), (1e9, 85.0), (float("nan"), 85.0), (True, 85.0),
                                    ("x", 85.0), (60, 60.0)])
def test_arg_thermal_max(v, want):
    assert g.Governor(4, thermal_max_c=v, readers=fake()).thermal_max_c == want


def test_negative_thermal_no_longer_pins_floor():
    gov = g.Governor(4, min_parallel=1, thermal_max_c=-5, readers=fake(temp_c=50.0))
    assert gov.thermal_max_c == 85.0
    assert not gov.is_hot()
    assert gov.target() == 4


@pytest.mark.parametrize("v,want", [(1e300, 300.0), (float("inf"), 2.0), (-5, 0.0), (True, 2.0), ("x", 2.0),
                                    (1.5, 1.5), (0, 0.0)])
def test_ttl_bounded(v, want):
    assert g.Governor(4, ttl_s=v, readers=fake()).ttl_s == want


def test_huge_ttl_still_refreshes():
    t = [0.0]
    temp = [40.0]
    gov = g.Governor(4, ttl_s=1e300, readers=fake(temp_c=None) | {"temp_c": lambda: temp[0]},
                     clock=lambda: t[0])
    assert gov.read().temp_c == 40.0
    temp[0] = 99.0
    t[0] = 301.0
    assert gov.read().temp_c == 99.0


@pytest.mark.parametrize("v,want", [("false", False), ("off", False), (" OFF ", False), ("no", False),
                                    ("on", True), ("true", True), ("", True), (False, False), (True, True),
                                    (0, False), (1, True)])
def test_enabled_text(v, want):
    assert g.Governor(4, enabled=v, readers=fake()).enabled is want


@pytest.mark.parametrize("raw,want", [(" off ", False), ("'false'", False), ("OFF", False), ("", True),
                                      ("on", True), ("yes", True)])
def test_from_settings_env_governor(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_GOVERNOR", raw)
    assert g.Governor.from_settings(NS(), readers=fake()).enabled is want


@pytest.mark.parametrize("v,want", [("off", False), ("false", False), ("on", True)])
def test_from_settings_text_attr(v, want):
    assert g.Governor.from_settings(NS(governor=v), readers=fake()).enabled is want


@pytest.mark.parametrize("dd", ["", "   ", 5, True, None, ["x"]])
def test_from_settings_odd_data_dir(dd):
    gov = g.Governor.from_settings(NS(data_dir=dd), readers=fake())
    assert gov.max_parallel >= 1


# ------------------------------------------------------------------ lesson_forget
def _ctx(dd):
    return NS(core=NS(settings=NS(data_dir=dd), codex=None), run_id="r1", name="a", role="")


@pytest.mark.parametrize("args", [["x"], "key", None, 5, ("k",)])
def test_forget_non_dict_args(args, tmp_path):
    out = asyncio.run(lf.t_lesson_forget(_ctx(str(tmp_path)), args))
    assert out.startswith("ERROR: key")


@pytest.mark.parametrize("reason", ["\u2062\u2063", "\ufe0f", "\u180e", "\u2061\u2064", "\u0301\u0301",
                                    "\u3164", "\u2800\u2800", "\u200b \u2060", "\U000e0020\U000e0041"])
def test_forget_invisible_reason_refused(reason, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    out = asyncio.run(lf.t_lesson_forget(_ctx(str(tmp_path)), {"key": KEY, "reason": reason}))
    assert out.startswith("ERROR: reason is required"), out


@pytest.mark.parametrize("reason", ["stale", "caf\u00e9 changed", "\u65e7\u306e", "\U0001f6ab wrong"])
def test_forget_visible_reason_accepted(reason, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    out = asyncio.run(lf.t_lesson_forget(_ctx(str(tmp_path)), {"key": KEY, "reason": reason}))
    assert out.startswith("ERROR: no lesson"), out


@pytest.mark.parametrize("dd", ["", "   ", "\t", 5, True, ["x"]])
def test_forget_unusable_data_dir(dd, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    monkeypatch.chdir(tmp_path)
    out = asyncio.run(lf.t_lesson_forget(_ctx(dd), {"key": KEY, "reason": "stale"}))
    assert "no data_dir" in out
    assert os.listdir(tmp_path) == []


def test_forget_zalgo_reason_calmed(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    from avrone import bridge as b
    d = str(tmp_path)
    b._store(d).save(b.LESSONS_NS, KEY, "use docker", run_id="r0", task="t")
    seen = {}
    codex = NS(emit=lambda run_id, kind, agent, **data: seen.update(data))
    ctx = NS(core=NS(settings=NS(data_dir=d), codex=codex), run_id="r1", name="a", role="")
    monkeypatch.setattr(b, "for_core", lambda core, run_id: NS(post=lambda *a, **k: {"id": "p1"}))
    out = asyncio.run(lf.t_lesson_forget(ctx, {"key": KEY, "reason": "x" + "\u0301" * 250}))
    assert out.startswith("forgot lesson"), out
    assert seen["reason"].count("\u0301") == 4


def test_reason_calm_unit():
    from avrone.tools_lessonrecall import _calm
    assert _calm("x" + "\u0301" * 300).count("\u0301") == 4


@pytest.mark.parametrize("ctx", [NS(run_id="r"), NS(), NS(core=None)])
def test_bridge_status_without_core(ctx):
    out = asyncio.run(lf.t_bridge_status(ctx, {}))
    assert isinstance(out, str) and out
