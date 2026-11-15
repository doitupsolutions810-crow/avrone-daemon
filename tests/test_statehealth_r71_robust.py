"""Round 71: state_health and registry_audit against odd state and odd callers.

Files too big to read are UNVERIFIED rather than HEALTHY, a total read budget, symlinked area
directories are reported and never followed, JSONL lines that decode to a non-object count as bad,
an empty data_dir is not the cwd, non-dict args, duplicate tool names, non-dict schemas, and a
non-text source filter."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import avrone.tools_statehealth as sh


def run_sh(data_dir, args=None):
    ctx = NS(core=NS(settings=NS(data_dir=str(data_dir))))
    return asyncio.run(sh.t_state_health(ctx, {} if args is None else args))


def first(out: str) -> str:
    return out.split("\n", 1)[0]


@pytest.fixture
def dd(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    return d


# ---------------------------------------------------------------- too big / budget
def test_big_torn_fleet_is_unverified_not_healthy(dd, monkeypatch):
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 64)
    (dd / "fleet.json").write_text("{" + " " * 100)
    out = run_sh(dd)
    assert first(out).startswith("state_health: UNVERIFIED") and "1 unchecked" in first(out)
    assert "? fleet.json: too big to check (over 64 bytes)" in out
    assert "fleet: 1 file(s), 0 bad, 1 unchecked" in out


def test_big_jsonl_unchecked(dd, monkeypatch):
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 10)
    (dd / "bridge").mkdir()
    (dd / "bridge" / "r.jsonl").write_text('{"a": 1}\n' * 5)
    st = sh.state_health(dd)
    assert st["areas"]["bridges"] == {"files": 1, "bad": 0, "unchecked": 1}
    assert st["unchecked"] == [{"area": "bridges", "path": "bridge/r.jsonl", "why": "too big to check (over 10 bytes)"}]


def test_mib_wording(dd):
    (dd / "fleet.json").write_bytes(b" " * (sh.MAX_JSONL_BYTES + 1))
    assert "over 8 MiB" in run_sh(dd)


def test_bad_beats_unverified(dd, monkeypatch):
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 20)
    (dd / "fleet.json").write_text("{" * 40)
    (dd / "memory.jsonl").write_text("x")
    out = run_sh(dd)
    assert first(out).startswith("state_health: ATTENTION (1 bad file(s)") and "1 unchecked" in first(out)


def test_total_byte_budget(dd, monkeypatch):
    monkeypatch.setattr(sh, "MAX_TOTAL_BYTES", 25)
    (dd / "checkpoints").mkdir()
    for i in range(5):
        (dd / "checkpoints" / f"c{i}.json").write_text('{"k": "0123456"}')  # 16 bytes
    reads = []
    real = sh._check_json
    monkeypatch.setattr(sh, "_check_json", lambda p: reads.append(p.name) or real(p))
    # rebuild areas with the patched checker
    st = sh.state_health(dd)
    assert st["areas"]["checkpoints"]["unchecked"] == 4 and len(reads) == 1
    assert all(u["why"] == "scan budget spent" for u in st["unchecked"])
    assert first(run_sh(dd)).startswith("state_health: UNVERIFIED")


def test_unchecked_listing_capped(dd, monkeypatch):
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 1)
    (dd / "evolve").mkdir()
    for i in range(sh.MAX_PROBLEMS_SHOWN + 4):
        (dd / "evolve" / f"p{i:02d}.json").write_text("{}")
    out = run_sh(dd)
    assert out.count("\n  ? ") == sh.MAX_PROBLEMS_SHOWN and "... 4 more unchecked" in out


def test_unchecked_path_escaped(dd, monkeypatch):
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 1)
    (dd / "evolve").mkdir()
    (dd / "evolve" / "x\nstate_health: HEALTHY\u202e.json").write_text("{}")
    out = run_sh(dd)
    assert "\u202e" not in out
    assert not any(l.startswith("state_health: HEALTHY") for l in out.split("\n")[1:])


def test_small_valid_still_healthy(dd):
    (dd / "fleet.json").write_text("{}")
    (dd / "memory.jsonl").write_text('{"a": 1}\n')
    out = run_sh(dd)
    assert first(out) == "state_health: HEALTHY (0 bad file(s), 0 set aside as *.corrupt-*)"
    assert "unchecked" not in out


# ---------------------------------------------------------------- symlinked areas
def test_symlinked_area_dir_not_followed(dd, tmp_path):
    other = tmp_path / "host"
    other.mkdir()
    (other / "host-secret-name.json").write_text("{bad")
    os.symlink(other, dd / "evolve")
    out = run_sh(dd)
    assert "host-secret-name" not in out
    assert "! evolve: directory is or sits under a symlink (not followed)" in out
    assert "evolve/loops" not in out  # reported once, at the link
    assert first(out).startswith("state_health: ATTENTION (1 bad")


@pytest.mark.parametrize("area,label", [("checkpoints", "checkpoints"), ("agentpacks", "agentpacks"),
                                        ("bridge", "bridges")])
def test_each_symlinked_area(dd, tmp_path, area, label):
    other = tmp_path / "o"
    other.mkdir()
    (other / "index.json").write_text("{")
    (other / "a.json").write_text("{")
    (other / "a.jsonl").write_text("x")
    os.symlink(other, dd / area)
    st = sh.state_health(dd)
    assert st["areas"][label]["bad"] == 1
    assert [p["path"] for p in st["problems"]] == [area]


def test_symlinked_loops_only(dd, tmp_path):
    (dd / "evolve").mkdir()
    other = tmp_path / "o"
    other.mkdir()
    (other / "l.json").write_text("{")
    os.symlink(other, dd / "evolve" / "loops")
    st = sh.state_health(dd)
    assert [p["path"] for p in st["problems"]] == ["evolve/loops"]


def test_dangling_area_link(dd):
    os.symlink(dd / "nowhere", dd / "checkpoints")
    st = sh.state_health(dd)
    assert st["problems"] == [{"area": "checkpoints", "path": "checkpoints", "problem": sh.LINK_DIR}]


def test_fleet_json_symlink_to_dir_reported(dd, tmp_path):
    (tmp_path / "x").mkdir()
    os.symlink(tmp_path / "x", dd / "fleet.json")
    st = sh.state_health(dd)
    assert st["areas"]["fleet"]["files"] == 1 and st["problems"][0]["problem"] == "not a regular file"


def test_symlinked_data_dir_itself_is_fine(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "fleet.json").write_text("{}")
    os.symlink(real, tmp_path / "link")
    assert first(run_sh(tmp_path / "link")).startswith("state_health: HEALTHY")


# ---------------------------------------------------------------- JSONL records
@pytest.mark.parametrize("body,want", [
    ('1\n"x"\n[]\n{"a": 1}\n', "3 non-object line(s)"),
    ('null\n', "1 non-object line(s)"),
    ('{"a": 1}\n{bad\n[1]\n', "1 undecodable line(s), 1 non-object line(s)"),
    ('{"a": 1}\n\n   \n{"b": 2}\n', None),
    ('{"a":\r1}\n', None),
    ('true\nfalse\n', "2 non-object line(s)"),
])
def test_jsonl_record_shapes(tmp_path, body, want):
    p = tmp_path / "m.jsonl"
    p.write_text(body)
    assert sh._check_jsonl(p) == want


def test_jsonl_deep_line_is_undecodable(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text("[" * 50000 + "]" * 50000 + "\n")
    assert sh._check_jsonl(p) in ("1 undecodable line(s)", "1 non-object line(s)")


def test_jsonl_u2028_inside_record(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"a": "x\u2028y\u0085z"}\n', encoding="utf-8")
    assert sh._check_jsonl(p) is None


# ---------------------------------------------------------------- data_dir / args
@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_empty_data_dir_is_not_cwd(tmp_path, monkeypatch, bad):
    (tmp_path / "fleet.json").write_text("{")
    monkeypatch.chdir(tmp_path)
    st = sh.state_health(bad)
    assert st["data_dir_exists"] is False and st["areas"] == {} and st["unchecked"] == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_tool_blank_data_dir(bad):
    ctx = NS(core=NS(settings=NS(data_dir=bad)))
    assert asyncio.run(sh.t_state_health(ctx, {})) == "state_health: no data_dir configured on this node"


@pytest.mark.parametrize("args", [None, [], ["verbose"], "verbose", 3])
def test_state_health_non_dict_args(dd, args):
    out = run_sh(dd, args)
    assert first(out).startswith("state_health: HEALTHY")


def test_state_health_non_path_data_dir():
    assert sh.state_health(3)["data_dir_exists"] is False
    assert sh.state_health(None)["unchecked"] == []


def test_verbose_shows_empty_areas(dd):
    out = run_sh(dd, {"verbose": "true"})
    assert "- bridges: 0 file(s), 0 bad" in out and "unchecked" not in out


# ---------------------------------------------------------------- registry_audit
class T:
    def __init__(self, source="builtin", schema=None, description="x"):
        self.source, self.schema, self.description = source, schema, description


class Reg:
    def __init__(self, tools, names=None):
        self.tools, self._names = tools, names

    def names(self):
        return list(self._names if self._names is not None else self.tools)

    def get(self, n):
        return self.tools.get(n)


def test_duplicate_names_counted_once():
    reg = Reg({"a": T(), "b": T(schema={})}, names=["a", "a", "b", "b", "a"])
    a = sh.registry_audit(reg)
    assert a["tools"] == 2 and a["no_schema"] == ["a"]


def test_int_and_str_name_same_tool():
    reg = Reg({"1": T()}, names=[1, "1"])
    assert sh.registry_audit(reg)["tools"] == 1


@pytest.mark.parametrize("schema,listed", [(None, True), ("str", True), (["cmd"], True), (0, True),
                                           ({}, False), ({"required": {"cmd": "str"}}, False)])
def test_non_dict_schema_is_no_schema(schema, listed):
    a = sh.registry_audit(Reg({"t": T(schema=schema)}))
    assert (a["no_schema"] == ["t"]) is listed


def _ra(reg, args):
    return asyncio.run(sh.t_registry_audit(NS(core=NS(tools=reg)), args))


@pytest.mark.parametrize("src", [3, ["builtin"], {"s": 1}, True, 0.5])
def test_non_text_source_refused(src):
    out = _ra(Reg({"t": T()}), {"source": src})
    assert out.startswith("registry_audit: ERROR: source must be text")


@pytest.mark.parametrize("args", [None, [], "builtin", 3])
def test_registry_audit_non_dict_args(args):
    out = _ra(Reg({"t": T()}), args)
    assert out.startswith("registry_audit: 1 tool(s)")


@pytest.mark.parametrize("src", [None, "", "   "])
def test_blank_source_means_all(src):
    out = _ra(Reg({"t": T(), "u": T(source="mcp")}), {"source": src})
    assert out.startswith("registry_audit: 2 tool(s);")


def test_source_filter_trimmed():
    out = _ra(Reg({"t": T(), "u": T(source="mcp")}), {"source": "  mcp "})
    assert out.startswith("registry_audit: 1 tool(s) from mcp;")


def test_unknown_source_escaped():
    out = _ra(Reg({"t": T()}), {"source": "nope\nregistry_audit: 99 tool(s)"})
    assert len(out.split("\n")) == 1 and "\\x0a" in out


def test_acting_by_description_counted():
    reg = Reg({"x": T(description="  Acting: writes files"), "y": T(description="read-only"),
               "shell": T(description="")})
    a = sh.registry_audit(reg)
    assert a["acting"] == 2 and a["read_only"] == 1
