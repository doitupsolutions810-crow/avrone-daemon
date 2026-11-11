"""Evolution gates use the running node's Python while preserving operator overrides."""
import asyncio
import json
import os
import shlex
import sys

import pytest

from avrone import evolve
from tests.test_evolve import mkctx, repo


def proposal(tmp_path, monkeypatch, **args):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo(tmp_path)))
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "allow")
    # Nested pytest verifies a tiny repository; installed optional plugins are unrelated.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    ctx = mkctx(tmp_path, tmp_path / "repo")
    result = asyncio.run(evolve.t_evolve_propose(ctx, {
        "goal": "add a helper", "edits": [{"path": "pkg/util.py", "content": "VALUE = 1\n"}],
        **args,
    }))
    assert "ACCEPTED" in result, result
    metadata = list((tmp_path / "d" / "evolve").glob("*.json"))
    assert len(metadata) == 1
    return json.loads(metadata[0].read_text())


def test_default_gate_uses_node_interpreter_when_path_python_is_wrong(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    tools = tmp_path / "path-tools"
    tools.mkdir()
    wrong_python = tools / "python"
    wrong_python.write_text("#!/bin/sh\necho wrong-python >&2\nexit 97\n")
    wrong_python.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    metadata = proposal(tmp_path, monkeypatch)
    assert shlex.split(metadata["test_cmd"])[0] == sys.executable
    assert "wrong-python" not in metadata["test_tail"]


def test_default_gate_quotes_interpreter_shell_metacharacters(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    actual_python = sys.executable
    launcher = tmp_path / "python 'node' $(touch UNQUOTED).bin"
    marker = tmp_path / "interpreter-ran"
    launcher.write_text("#!/bin/sh\nprintf ran > " + shlex.quote(str(marker)) +
                        "\nexec " + shlex.quote(actual_python) + ' "$@"\n')
    launcher.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(launcher))
    metadata = proposal(tmp_path, monkeypatch)
    assert marker.read_text() == "ran"
    assert shlex.split(metadata["test_cmd"])[0] == str(launcher)
    assert not list(tmp_path.rglob("UNQUOTED"))


@pytest.mark.parametrize("explicit", [False, True])
def test_custom_gate_commands_remain_operator_controlled(tmp_path, monkeypatch, explicit):
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", "printf environment-gate")
    supplied = {"test_cmd": "printf explicit-gate"} if explicit else {}
    metadata = proposal(tmp_path, monkeypatch, **supplied)
    expected = "printf explicit-gate" if explicit else "printf environment-gate"
    assert metadata["test_cmd"] == expected
    assert metadata["test_tail"] == expected.removeprefix("printf ")
