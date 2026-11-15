"""Round 77: blank directory settings never mean the process cwd."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from avrone import agentkit, capgate, config, fusion_blackboard, fusion_peer, plugins, tools_secrets

BLANKS = ["", " ", "\t", "\n", "  \t "]


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    d = tmp_path / "cwd"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


@pytest.mark.parametrize("blank", BLANKS)
@pytest.mark.parametrize("var,attr,default", [
    ("AVRONE_DATA_DIR", "data_dir", "data"), ("AVRONE_WORKSPACE", "workspace", "workspace")])
def test_settings_blank_dir_is_default(blank, var, attr, default, cwd, monkeypatch):
    monkeypatch.setenv(var, blank)
    got = getattr(config.Settings(), attr)
    assert got == (cwd / default).resolve()
    assert got != cwd.resolve()


@pytest.mark.parametrize("var,attr", [("AVRONE_DATA_DIR", "data_dir"), ("AVRONE_WORKSPACE", "workspace")])
def test_settings_unset_is_default(var, attr, cwd, monkeypatch):
    monkeypatch.delenv(var, raising=False)
    assert getattr(config.Settings(), attr).parent == cwd.resolve()


def test_settings_tilde_expanded(cwd, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AVRONE_DATA_DIR", "~/avdata")
    assert config.Settings().data_dir == (tmp_path / "home" / "avdata").resolve()


def test_settings_explicit_path_kept(cwd, monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "x"))
    assert config.Settings().data_dir == (tmp_path / "x").resolve()


def test_settings_relative_path_kept(cwd, monkeypatch):
    monkeypatch.setenv("AVRONE_DATA_DIR", "rel/d")
    assert config.Settings().data_dir == (cwd / "rel" / "d").resolve()


@pytest.mark.parametrize("blank", BLANKS)
def test_plugins_dir_blank_is_default(blank, cwd, monkeypatch):
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", blank)
    assert plugins.plugins_dir() == (cwd / "plugins").resolve()


def test_plugins_dir_explicit(cwd, monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(tmp_path / "pl"))
    assert plugins.plugins_dir() == (tmp_path / "pl").resolve()


def test_plugins_dir_tilde(cwd, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", "~/pl")
    assert plugins.plugins_dir() == (tmp_path / "h" / "pl").resolve()


@pytest.mark.parametrize("blank", BLANKS)
def test_skills_dir_blank_is_default(blank, cwd, monkeypatch):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", blank)
    got = agentkit._skills_dir(None)
    assert got.name == "skills" and got.is_absolute() and got.parent != cwd


def test_skills_dir_unset_matches_blank(cwd, monkeypatch):
    monkeypatch.delenv("AVRONE_SKILLS_DIR", raising=False)
    a = agentkit._skills_dir(None)
    monkeypatch.setenv("AVRONE_SKILLS_DIR", "")
    assert agentkit._skills_dir(None) == a


def test_skills_dir_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(tmp_path / "sk"))
    assert agentkit._skills_dir(None) == tmp_path / "sk"


def ctx_dd(dd, ws=None):
    return NS(core=NS(settings=NS(data_dir=dd, workspace=ws), runner=NS(workspace=ws)))


@pytest.mark.parametrize("dd", BLANKS + [None, 5, {}])
def test_capgate_data_dir_of_blank(dd):
    assert capgate.data_dir_of(ctx_dd(dd)) is None


def test_capgate_data_dir_of_real(tmp_path):
    assert capgate.data_dir_of(ctx_dd(str(tmp_path))) == tmp_path


@pytest.mark.parametrize("mod", [fusion_blackboard, fusion_peer])
@pytest.mark.parametrize("dd", BLANKS + [5, {"a": 1}])
def test_fusion_data_dir_blank(mod, dd):
    assert mod._data_dir(NS(settings=NS(data_dir=dd))) is None


@pytest.mark.parametrize("mod", [fusion_blackboard, fusion_peer])
def test_fusion_data_dir_real(mod, tmp_path):
    assert mod._data_dir(NS(settings=NS(data_dir=str(tmp_path)))) == tmp_path


@pytest.mark.parametrize("dd", BLANKS + [3])
@pytest.mark.parametrize("scope", ["data", "runs"])
def test_secrets_roots_blank_data_dir(dd, scope):
    assert tools_secrets._roots(ctx_dd(dd), scope) is None


@pytest.mark.parametrize("ws", BLANKS + [7])
def test_secrets_roots_blank_workspace(ws):
    assert tools_secrets._roots(ctx_dd(None, ws), "workspace") is None


def test_secrets_roots_runner_blank_falls_back_to_settings(tmp_path):
    ctx = NS(core=NS(settings=NS(data_dir=None, workspace=str(tmp_path)), runner=NS(workspace="  ")))
    assert tools_secrets._roots(ctx, "workspace") == tmp_path


def test_secrets_roots_real(tmp_path):
    assert tools_secrets._roots(ctx_dd(str(tmp_path)), "runs") == tmp_path / "runs"
    assert tools_secrets._roots(ctx_dd(None, str(tmp_path)), "workspace") == tmp_path
