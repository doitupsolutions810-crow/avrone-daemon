"""Round 89: path settings read like secrets (quotes/padding cleaned, quoted blank = default),
governor thermal-limit mark capped at 99, vm_stat/pmset/cgroup parsers bounded, node id NFKC."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from avrone import agentcontainer, agentkit, config, plugins
from avrone import governor as gv

QUOTED = ['"{p}"', "'{p}'", "  {p}  ", ' "{p}" ', "\t{p}\n"]
BLANKS = ["", "   ", '""', "''", ' "" ', "\t\n", '"  "']


# ------------------------------------------------------------------ env_path helpers
@pytest.mark.parametrize("shape", QUOTED)
def test_env_path_text_cleans(shape):
    assert config.env_path_text(shape.format(p="/srv/avrone"), "./d") == "/srv/avrone"


@pytest.mark.parametrize("blank", BLANKS + [None, 5, b"/x", ["/x"]])
def test_env_path_text_blank_is_default(blank):
    assert config.env_path_text(blank, "./data") == "./data"


def test_env_path_expands_tilde():
    assert config.env_path("'~/av'", "./d") == Path("~/av").expanduser()


def test_env_path_keeps_inner_quotes_and_spaces():
    assert config.env_path_text('"/srv/my dir"', "x") == "/srv/my dir"
    assert config.env_path_text("/srv/it's", "x") == "/srv/it's"


def test_env_path_unmatched_quote_kept():
    assert config.env_path_text('"/srv/x', "d") == '"/srv/x'


# ------------------------------------------------------------------ Settings dirs
@pytest.mark.parametrize("name,field", [("AVRONE_DATA_DIR", "data_dir"), ("AVRONE_WORKSPACE", "workspace")])
@pytest.mark.parametrize("shape", QUOTED)
def test_settings_dirs_cleaned(monkeypatch, tmp_path, name, field, shape):
    target = tmp_path / "x"
    monkeypatch.setenv(name, shape.format(p=str(target)))
    assert config._path(name, "./d")() == target.resolve()


@pytest.mark.parametrize("blank", BLANKS)
def test_settings_dirs_quoted_blank_default(monkeypatch, blank):
    monkeypatch.setenv("AVRONE_DATA_DIR", blank)
    assert config._path("AVRONE_DATA_DIR", "./data")() == Path("./data").resolve()


def test_settings_dir_no_quote_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_DATA_DIR", f'"{tmp_path}/d"')
    p = config._path("AVRONE_DATA_DIR", "./data")()
    assert '"' not in str(p)


def test_settings_dir_tilde(monkeypatch):
    monkeypatch.setenv("AVRONE_WORKSPACE", '"~/avws"')
    assert config._path("AVRONE_WORKSPACE", "./w")() == (Path.home() / "avws").resolve()


# ------------------------------------------------------------------ env_file
@pytest.mark.parametrize("blank", BLANKS)
def test_env_file_quoted_blank_is_dotenv(monkeypatch, blank):
    monkeypatch.setenv("AVRONE_ENV_FILE", blank)
    assert config.env_file() == Path(".env")


def test_env_file_unset(monkeypatch):
    monkeypatch.delenv("AVRONE_ENV_FILE", raising=False)
    assert config.env_file() == Path(".env")


@pytest.mark.parametrize("shape", QUOTED)
def test_env_file_cleaned(monkeypatch, shape):
    monkeypatch.setenv("AVRONE_ENV_FILE", shape.format(p="/etc/avrone.env"))
    assert config.env_file() == Path("/etc/avrone.env")


def test_env_file_never_a_directory(monkeypatch):
    monkeypatch.setenv("AVRONE_ENV_FILE", '""')
    assert str(config.env_file()) not in ("", ".")


# ------------------------------------------------------------------ skills dir
DEFAULT_SKILLS = Path(agentkit.__file__).resolve().parent.parent / "skills"


@pytest.mark.parametrize("blank", BLANKS)
def test_skills_dir_blank_default(monkeypatch, blank):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", blank)
    assert agentkit._skills_dir(None) == DEFAULT_SKILLS


def test_skills_dir_unset(monkeypatch):
    monkeypatch.delenv("AVRONE_SKILLS_DIR", raising=False)
    assert agentkit._skills_dir(None) == DEFAULT_SKILLS


@pytest.mark.parametrize("shape", QUOTED)
def test_skills_dir_cleaned(monkeypatch, shape):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", shape.format(p="/opt/skills"))
    assert agentkit._skills_dir(None) == Path("/opt/skills")


def test_skills_dir_tilde(monkeypatch):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", "'~/sk'")
    assert agentkit._skills_dir(None) == Path.home() / "sk"


# ------------------------------------------------------------------ plugins dir
@pytest.mark.parametrize("blank", BLANKS)
def test_plugins_dir_blank_default(monkeypatch, blank):
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", blank)
    assert plugins.plugins_dir() == Path("./plugins").resolve()


@pytest.mark.parametrize("shape", QUOTED)
def test_plugins_dir_cleaned(monkeypatch, tmp_path, shape):
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", shape.format(p=str(tmp_path / "pl")))
    assert plugins.plugins_dir() == (tmp_path / "pl").resolve()


def test_plugins_dir_quoted_not_cwd_subfolder(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", f'"{tmp_path}"')
    assert plugins.plugins_dir() == tmp_path.resolve()


# ------------------------------------------------------------------ thermal limit mark
@pytest.mark.parametrize("v,want", [("100", 99.0), ("99", 99.0), ("1e9", 99.0), ("inf", 80.0),
                                    ('"100"', 99.0), ("50", 50.0), ("-3", 1.0), ("", 80.0)])
def test_thermal_limit_hot_bounds(monkeypatch, v, want):
    monkeypatch.setenv("AVRONE_THERMAL_LIMIT_HOT_PCT", v)
    assert gv.Governor(2, readers={}).thermal_limit_hot == want


def test_full_speed_mac_never_hot_even_at_max_mark(monkeypatch):
    monkeypatch.setenv("AVRONE_THERMAL_LIMIT_HOT_PCT", "100")
    g = gv.Governor(4, readers={"temp_c": lambda: None, "thermal_limit_pct": lambda: 100.0})
    assert not g.is_hot()


def test_throttled_mac_hot_at_max_mark(monkeypatch):
    monkeypatch.setenv("AVRONE_THERMAL_LIMIT_HOT_PCT", "100")
    g = gv.Governor(4, readers={"temp_c": lambda: None, "thermal_limit_pct": lambda: 99.0})
    assert g.is_hot()


def test_max_constant():
    assert gv.MAX_THERMAL_LIMIT_HOT == 99.0 < 100.0


# ------------------------------------------------------------------ pmset parser
@pytest.mark.parametrize("text,want", [
    ("CPU_Speed_Limit = 70", 70.0),
    ("CPU_Speed_Limit\t=\t55", 55.0),
    ("CPU_Speed_Limit = \u0663\u0660", None),           # Arabic-Indic digits are not a reading
    ("CPU_Speed_Limit = \uff17\uff10", None),            # fullwidth
    ("CPU_Speed_Limit =\n70", None),                     # value on another line
    ("CPU_Scheduler_Limit = 40\nCPU_Speed_Limit = 90", 40.0),
    ("CPU_Speed_Limit = 999", 100.0),
    ("", None),
])
def test_pmset_parse(text, want):
    assert gv.parse_pmset_therm(text) == want


@pytest.mark.parametrize("bad", [None, b"CPU_Speed_Limit = 50", 12, ["x"]])
def test_pmset_non_text(bad):
    assert gv.parse_pmset_therm(bad) is None


# ------------------------------------------------------------------ vm_stat parser
def _vm(page="16384", free="100", inactive="100", extra=""):
    return (f"Mach Virtual Memory Statistics: (page size of {page} bytes)\n"
            f"Pages free:  {free}.\nPages inactive: {inactive}.\n{extra}")


def test_vm_stat_normal():
    assert gv.parse_vm_stat(_vm(), 16384 * 1000) == pytest.approx(80.0)


def test_vm_stat_intel_page():
    assert gv.parse_vm_stat(_vm(page="4096"), 4096 * 1000) == pytest.approx(80.0)


@pytest.mark.parametrize("page", ["0", "1", "3000", "99999999999999999999", "16385", "2097152"])
def test_vm_stat_bad_page_size_falls_back_to_4096(page):
    assert gv.parse_vm_stat(_vm(page=page), 4096 * 1000) == pytest.approx(80.0)


def test_vm_stat_zero_page_not_full_memory():
    assert gv.parse_vm_stat(_vm(page="0"), 4096 * 1000) < 95.0


@pytest.mark.parametrize("total", [0, -1, float("nan"), float("inf"), 1.5e9, True, "1000", None])
def test_vm_stat_bad_total_is_no_reading(total):
    assert gv.parse_vm_stat(_vm(), total) is None


@pytest.mark.parametrize("bad", [None, b"Pages free: 1.", 7])
def test_vm_stat_non_text(bad):
    assert gv.parse_vm_stat(bad, 1 << 30) is None


def test_vm_stat_unicode_line_separator_not_a_line():
    # \u2028 must not split a line: "Pages free" stays glued to the junk and is not read
    t = "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nJunk: 1\u2028Pages free: 999999.\n"
    assert gv.parse_vm_stat(t, 4096 * 1000) is None


def test_vm_stat_non_ascii_digits_ignored():
    t = _vm(free="\u0661\u0660\u0660")
    assert gv.parse_vm_stat(t, 16384 * 1000) is None


def test_vm_stat_crlf_ok():
    t = _vm().replace("\n", "\r\n")
    assert gv.parse_vm_stat(t, 16384 * 1000) == pytest.approx(80.0)


def test_vm_stat_count_must_fill_value():
    assert gv.parse_vm_stat(_vm(free="100 extra"), 16384 * 1000) is None


def test_darwin_mem_pct_zero_page_header():
    outs = {("sysctl", "-n", "hw.memsize"): str(4096 * 1000),
            ("vm_stat",): _vm(page="0"),
            ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "1"}
    pct = gv.darwin_mem_pct(run=lambda argv: outs.get(tuple(argv)))
    assert pct == pytest.approx(80.0)


# ------------------------------------------------------------------ cgroup inactive_file
def _cg(tmp_path, stat):
    (tmp_path / "memory.max").write_text("1000\n")
    (tmp_path / "memory.current").write_text("800\n")
    (tmp_path / "memory.stat").write_text(stat)
    return tmp_path


def _read_cg(root):
    return gv.cgroup_mem_pct(str(root))


def test_cgroup_inactive_subtracted(tmp_path):
    assert _read_cg(_cg(tmp_path, "inactive_file 300\n")) == pytest.approx(50.0)


def test_cgroup_inactive_value_on_next_line_ignored(tmp_path):
    assert _read_cg(_cg(tmp_path, "inactive_file\n300\n")) == pytest.approx(80.0)


def test_cgroup_inactive_non_ascii_ignored(tmp_path):
    assert _read_cg(_cg(tmp_path, "inactive_file \u0663\u0660\u0660\n")) == pytest.approx(80.0)


# ------------------------------------------------------------------ node id
@pytest.mark.parametrize("raw,want", [
    ("\uff2d\uff21\uff23\uff11", "mac1"),
    ('"mac-1"', "mac-1"),
    ("  Studio  ", "studio"),
    ("\ufb01ber", "fiber"),
])
def test_node_id_nfkc(monkeypatch, raw, want):
    monkeypatch.setenv("AVRONE_NODE_ID", raw)
    assert agentcontainer.local_node_id() == want


def test_node_id_all_symbols_is_node(monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "\u2603\u2603")
    assert agentcontainer.local_node_id() == "node"


def test_node_id_capped(monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "a" * 200)
    assert len(agentcontainer.local_node_id()) == 40


def test_node_id_blank_uses_hostname(monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "   ")
    assert agentcontainer.local_node_id()
