"""Hardening round 55: tools_secrets (secret_scan + fix), plugins (load_plugins), greatscale."""
from __future__ import annotations

import asyncio
import json
import math
import sys
from types import SimpleNamespace as S

import pytest

from avrone import greatscale as gs
from avrone import plugins as pl
from avrone import tools_secrets as ts
from avrone.tools import Tool, ToolRegistry

GH1 = "ghp_" + "A" * 36
GH2 = "ghp_" + "B" * 36


def run(c):
    return asyncio.run(c)


def ctx(data_dir, ws=None):
    return S(core=S(settings=S(data_dir=data_dir, workspace=ws), runner=None, codex=None))


# ------------------------------------------------------------ _redact_value key collisions
def test_two_secret_keys_both_kept():
    out = ts._redact_value({GH1: 1, GH2: 2})
    assert len(out) == 2 and sorted(out.values()) == [1, 2]
    assert not any("AAAA" in k or "BBBB" in k for k in out)


def test_three_secret_keys_and_existing_suffix():
    v = {GH1: 1, GH2: 2, "ghp_" + "C" * 36: 3, "[REDACTED]#2": 9}
    out = ts._redact_value(v)
    assert len(out) == 4 and sorted(out.values()) == [1, 2, 3, 9] and out["[REDACTED]#2"] == 9


def test_plain_keys_untouched():
    assert ts._redact_value({"a": GH1, "b": [GH2]}) == {"a": "[REDACTED]", "b": ["[REDACTED]"]}


def test_fix_keeps_both_entries(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r.jsonl").write_text(json.dumps({"env": {GH1: "x", GH2: "y"}}) + "\n")
    out = run(ts.t_secret_scan(ctx(tmp_path), {"fix": True}))
    assert "0 line(s) still flagged" in out
    obj = json.loads((runs / "r.jsonl").read_text())
    assert sorted(obj["env"].values()) == ["x", "y"]


# ------------------------------------------------------------ JSON-escaped secrets
@pytest.fixture()
def quoted_secret(monkeypatch):
    v = 'Zq"9vB\\x7Lw2Kp4Hn'
    monkeypatch.setenv("ROUND55_API_TOKEN", v)
    return v


def test_escaped_env_secret_found(tmp_path, quoted_secret):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r.jsonl").write_text(json.dumps({"out": "value " + quoted_secret}) + "\n")
    out = run(ts.t_secret_scan(ctx(tmp_path), {}))
    assert "FOUND" in out and "json-escaped-secret" in out


def test_escaped_env_secret_fixed(tmp_path, quoted_secret):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r.jsonl").write_text(json.dumps({"out": quoted_secret}) + "\n")
    out = run(ts.t_secret_scan(ctx(tmp_path), {"fix": True}))
    assert "fixed 1 run log(s); 0 line(s) still flagged" in out
    assert quoted_secret not in json.loads((runs / "r.jsonl").read_text())["out"]


def test_list_and_indented_lines_checked(quoted_secret):
    assert ts._line_hides_secret("  " + json.dumps([quoted_secret]))
    assert ts._line_hides_secret(json.dumps([quoted_secret]))


def test_newline_escape_only_not_decoded():
    assert ts._line_hides_secret(json.dumps({"a": "x\ny"})) is False


def test_clean_escaped_line_stays_clean(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r.jsonl").write_text(json.dumps({"out": 'say "hi" \\ there'}) + "\n")
    assert "CLEAN" in run(ts.t_secret_scan(ctx(tmp_path), {}))


# ------------------------------------------------------------ file selection
@pytest.mark.parametrize("name", [".env.local", ".env.production", "prod.env.bak", ".ENV", "server.pem",
                                  "id.key", "app.conf", "db.properties", "prod.tfvars", "settings.xml"])
def test_secret_files_scanned(tmp_path, name):
    (tmp_path / name).write_text(f"TOKEN={GH1}\n")
    out = run(ts.t_secret_scan(ctx(tmp_path, ws=tmp_path), {"scope": "workspace"}))
    assert "FOUND" in out and "1 file(s)" in out


@pytest.mark.parametrize("name", ["photo.png", "a.bin", "lib.so"])
def test_binary_suffixes_skipped(tmp_path, name):
    (tmp_path / name).write_text(f"TOKEN={GH1}\n")
    assert "CLEAN" in run(ts.t_secret_scan(ctx(tmp_path, ws=tmp_path), {"scope": "workspace"}))


@pytest.mark.parametrize("scope", [" runs", "RUNS", "Data "])
def test_scope_spellings(tmp_path, scope):
    (tmp_path / "runs").mkdir()
    assert run(ts.t_secret_scan(ctx(tmp_path), {"scope": scope})).startswith("secret_scan [")


@pytest.mark.parametrize("scope", [["runs"], 5, None])
def test_bad_scope(tmp_path, scope):
    assert run(ts.t_secret_scan(ctx(tmp_path), {"scope": scope})).startswith("ERROR: scope")


# ------------------------------------------------------------ hit memory cap
def test_hits_capped_but_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "HITS_KEEP", 10)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "a.jsonl").write_text("".join(f'{{"t":"{GH1}"}}\n' for _ in range(40)))
    (runs / "b.jsonl").write_text("".join(f'{{"t":"{GH2}"}}\n' for _ in range(5)))
    res = ts.scan(runs)
    assert len(res["hits"]) == 10 and res["hits"].count == 45 and res["hits"].paths == {"a.jsonl", "b.jsonl"}
    out = run(ts.t_secret_scan(ctx(tmp_path), {}))
    assert "45 line(s) in 2 file(s)" in out and "... 15 more" in out


def test_fix_reaches_paths_past_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "HITS_KEEP", 3)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "a.jsonl").write_text("".join(f'{{"t":"{GH1}"}}\n' for _ in range(10)))
    (runs / "z.jsonl").write_text(f'{{"t":"{GH2}"}}\n')
    out = run(ts.t_secret_scan(ctx(tmp_path), {"fix": True}))
    assert "fixed 2 run log(s); 0 line(s) still flagged" in out


# ------------------------------------------------------------ plugins
def write(d, name, body):
    p = d / name
    p.write_text(body)
    return p


REG_OK = "from avrone.tools import Tool\n" \
         "async def f(ctx, args): return 'x'\n" \
         "def register(r, s): r.register(Tool('{n}', 'd', '{{}}', f))\n"


def test_dotted_and_dashed_names_load(tmp_path):
    write(tmp_path, "my.plugin.py", REG_OK.format(n="t_dot"))
    write(tmp_path, "my-plugin.py", REG_OK.format(n="t_dash"))
    reg = ToolRegistry()
    reps = pl.load_plugins(reg, directory=tmp_path, include_entry_points=False)
    assert all(r["error"] is None for r in reps), reps
    assert reg.get("t_dot") and reg.get("t_dash")


def test_file_and_package_same_stem_both_load(tmp_path):
    write(tmp_path, "foo.py", REG_OK.format(n="t_file"))
    (tmp_path / "foo.pkg").mkdir()
    write(tmp_path / "foo.pkg", "__init__.py", REG_OK.format(n="t_pkg"))
    reg = ToolRegistry()
    reps = pl.load_plugins(reg, directory=tmp_path, include_entry_points=False)
    names = sorted(r["name"] for r in reps)
    assert names == ["foo", "foo.pkg"] and reg.get("t_file") and reg.get("t_pkg")
    assert reg.get("t_pkg").source == "plugin:foo.pkg"


def test_existing_module_name_not_replaced(tmp_path):
    sentinel = object()
    sys.modules["avrone_plugin_keepme"] = sentinel
    try:
        write(tmp_path, "keepme.py", REG_OK.format(n="t_keep"))
        reps = pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False)
        assert reps[0]["error"] is None and sys.modules["avrone_plugin_keepme"] is sentinel
    finally:
        sys.modules.pop("avrone_plugin_keepme", None)


def test_custom_baseexception_in_register(tmp_path):
    write(tmp_path, "boom.py", "class B(BaseException): pass\ndef register(r, s): raise B('no')\n")
    reps = pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False)
    assert reps[0]["error"].startswith("B: no")


def test_custom_baseexception_at_import(tmp_path):
    write(tmp_path, "boom2.py", "class B(BaseException): pass\nraise B('top')\n")
    reps = pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False)
    assert reps[0]["error"].startswith("B: top")


def test_keyboardinterrupt_still_propagates(tmp_path):
    write(tmp_path, "ki.py", "def register(r, s): raise KeyboardInterrupt\n")
    with pytest.raises(KeyboardInterrupt):
        pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False)


def test_huge_error_clipped_and_escaped(tmp_path):
    write(tmp_path, "big.py", "def register(r, s): raise ValueError('x\\nFORGED\\u202e' + 'y' * 100000)\n")
    err = pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False)[0]["error"]
    assert len(err) <= pl.ERROR_MAX + 40 and "\n" not in err and "\u202e" not in err


def test_unprintable_exception(tmp_path):
    write(tmp_path, "up.py", "class E(Exception):\n    def __str__(self): raise RuntimeError\n"
                             "def register(r, s): raise E()\n")
    err = pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False)[0]["error"]
    assert err == "E: <unprintable>"


def test_unreadable_plugins_dir(tmp_path, monkeypatch):
    from pathlib import Path
    real = Path.iterdir

    def boom(self):
        if self == tmp_path.resolve():
            raise PermissionError("denied")
        return real(self)
    monkeypatch.setattr(Path, "iterdir", boom)
    reps = pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False)
    assert reps[0]["name"] == "(plugins dir)" and "PermissionError" in reps[0]["error"]


def test_init_dir_not_file_skipped(tmp_path):
    (tmp_path / "weird").mkdir()
    (tmp_path / "weird" / "__init__.py").mkdir()
    assert pl.load_plugins(ToolRegistry(), directory=tmp_path, include_entry_points=False) == []


# ------------------------------------------------------------ greatscale
def test_heat_nan_is_nan_not_floor():
    assert math.isnan(gs.heat(float("nan")))
    assert gs.cone_compliant(gs.heat(float("nan"))) is False


@pytest.mark.parametrize("h,want", [(0, 0), (1, 0), (0.5, 0.25), (-3, 0), (7, 0)])
def test_heat_clamps(h, want):
    assert gs.heat(h) == want


def test_rare_defect_runs_exact():
    p = 1e-9
    exact = math.ceil(math.log(0.05) / math.log1p(-p) - 1e-12)
    assert gs.runs_to_detect(p) == exact
    assert abs(gs.runs_to_detect(p) - 2995732273) <= 1


def test_tiny_p_no_zero_division():
    assert gs.runs_to_detect(1e-17) > 10 ** 17


@pytest.mark.parametrize("p,want", [(0.5, 5), (1 / 200, 598), (1, 1)])
def test_runs_known_values(p, want):
    assert gs.runs_to_detect(p) == want


def test_detect_in_runs_precise():
    snap = gs.evidence_snapshot(1, 10 ** 9)
    # (1 - 1e-9) ** 1e9 = exp(1e9 * log1p(-1e-9)) = exp(-1 - 5e-10 - ...)
    assert snap["detectInRuns"] == pytest.approx(1 - math.exp(-1 - 5e-10), rel=1e-12)
    assert gs.evidence_snapshot(3, 3)["detectInRuns"] == 1.0
    assert gs.evidence_snapshot(0, 3)["detectInRuns"] == 0.0


@pytest.mark.parametrize("alpha", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_alpha_refused(alpha):
    out = run(gs.t_great_scale(None, {"failures": 1, "runs": 10, "alpha": alpha}))
    assert out.startswith("ERROR: alpha")


@pytest.mark.parametrize("fmt", [" JSON ", "Json"])
def test_json_spellings(fmt):
    json.loads(run(gs.t_great_scale(None, {"failures": 1, "runs": 10, "format": fmt})))


def test_non_string_format_text():
    assert run(gs.t_great_scale(None, {"failures": 1, "runs": 10, "format": ["json"]})).startswith("great_scale")
