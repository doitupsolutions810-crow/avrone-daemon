"""Edge-case tests for scripts/mac-avrone-autopilot.sh.

Each test runs the real script against a throwaway copy of the repo, a fake drive and stubbed
`brew` / `launchctl` / `ollama` (a tiny HTTP server on a free port), with AVRONE in mock LLM mode
so lanes finish instantly. Marked `integration`: excluded from the self-evolve gate, run in CI.
"""
from __future__ import annotations

import re
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(sys.platform == "win32", reason="bash script")]

ROOT = Path(__file__).resolve().parents[1]
# run the whole suite under another bash (e.g. a build of macOS's stock bash 3.2.57)
BASH = os.environ.get("AUTOPILOT_TEST_BASH", "bash")
SCRIPT_REL = "scripts/mac-avrone-autopilot.sh"
BASE_COMMIT = "1121d26"   # upstream main before the carried patches

STUB_OLLAMA = r'''#!/usr/bin/env python3
import os, sys, json, http.server, threading
D = os.environ["STUB_DIR"]
def flag(n): return os.path.exists(os.path.join(D, n))
def log(m): open(os.path.join(D, "calls.log"), "a").write(m + "\n")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
log("ollama " + " ".join(sys.argv[1:]))
models = os.path.join(D, "models")
if cmd == "serve":
    if flag("serve_fail"): sys.exit(1)
    if flag("serve_race"):
        # another Ollama (the menu-bar app) grabs the port first; ours then dies with "address in use"
        os.remove(os.path.join(D, "serve_race"))
        import subprocess, time
        subprocess.Popen([sys.argv[0], "serve"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        print("Error: listen tcp: bind: address already in use", file=sys.stderr); sys.exit(1)
    open(os.path.join(D, "serve.pid"), "w").write(str(os.getpid()))
    host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
    maxreq = int(open(os.path.join(D, "max_req")).read()) if flag("max_req") else 0
    n = [0]
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            n[0] += 1
            if flag("serve_hang_after") and n[0] > int(open(os.path.join(D, "serve_hang_after")).read()):
                import time; time.sleep(600)
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"version":"stub"}')
            if maxreq and n[0] >= maxreq:
                threading.Thread(target=srv.shutdown).start()
        def log_message(self, *a): pass
    srv = http.server.HTTPServer((host, int(port)), H)
    srv.serve_forever(); sys.exit(0)
if cmd == "list":
    if flag("list_hang"):
        import time; time.sleep(300)
    if flag("list_fail"):
        print("Error: something went wrong, please see the ollama server logs", file=sys.stderr); sys.exit(1)
    if not flag("list_noheader"):
        print("NAME ID SIZE MODIFIED")
    if os.path.exists(models):
        for m in open(models).read().split():
            print(f"{m} abc 1GB now")
    sys.exit(0)
if cmd == "create":
    if flag("create_fail"): print("Error: create failed", file=sys.stderr); sys.exit(1)
    mf = sys.argv[sys.argv.index("-f") + 1]
    open(os.path.join(D, "created_modelfile"), "w").write(open(mf).read())
    open(models, "a").write(sys.argv[2] + "\n"); sys.exit(0)
if cmd == "pull":
    if flag("pull_msg"):
        print(open(os.path.join(D, "pull_msg")).read(), file=sys.stderr); sys.exit(1)
    if flag("pull_noop"): sys.exit(0)
    if flag("pull_fail"): sys.exit(1)
    open(models, "a").write(sys.argv[2] + "\n"); sys.exit(0)
if cmd == "run":
    if flag("run_silent"): sys.exit(0)
    if flag("run_hang"):
        import time; time.sleep(300)
    print("ready"); sys.exit(0)
sys.exit(0)
'''


STUB_PYTHON = r'''#!/bin/sh
# wraps the real interpreter; `python -m avrone.cli chat ...` can be made to misbehave via flag files
case " $* " in *" avrone.cli chat "*)
  S="$DRIVE/avrone-state"
  [ -e "$STUB_DIR/chat_sleep" ] && sleep "$(cat "$STUB_DIR/chat_sleep")"
  [ -e "$STUB_DIR/chat_set_stage" ] && { cat "$STUB_DIR/chat_set_stage" > "$S/reintro-stage.txt"; cat "$STUB_DIR/chat_set_stage" > "$S/reintro-stage.txt.bak"; }
  [ -e "$STUB_DIR/chat_delete_stage" ] && rm -f "$S/reintro-stage.txt"
  [ -e "$STUB_DIR/chat_corrupt_plan" ] && printf '{' > "$S/reintro-plan.json"
  # unplug right after the agent process ends (AVRONE itself may mkdir its data dir while running;
  # on a real Mac /Volumes is root-owned so that fails — what we test is that the autopilot never recreates it)
  if [ -e "$STUB_DIR/chat_unplug" ]; then "@PY@" "$@"; rc=$?; mv "$DRIVE" "$DRIVE.unplugged"; exit $rc; fi
  [ -e "$STUB_DIR/chat_remove_model" ] && : > "$STUB_DIR/models"
  [ -e "$STUB_DIR/chat_readonly_state" ] && chmod 555 "$S"
  if [ -e "$STUB_DIR/chat_flood" ]; then head -c 3000000 /dev/zero | tr '\0' 'x'; head -c 3000000 /dev/zero | tr '\0' 'y' >&2; fi
  [ -e "$STUB_DIR/chat_crash" ] && { echo "Traceback: boom" >&2; exit 3; }
  [ -e "$STUB_DIR/chat_rm_runs" ] && rm -rf "$DRIVE/avrone-runs"
  [ -e "$STUB_DIR/chat_git_reset" ] && git reset -q --hard "$(cat "$STUB_DIR/chat_git_reset")" >/dev/null 2>&1
  [ -e "$STUB_DIR/chat_git_commit" ] && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m "crow pulled" >/dev/null 2>&1
  [ -e "$STUB_DIR/chat_modnotfound" ] && { printf 'Traceback (most recent call last):\n  File "x"\nModuleNotFoundError: No module named %s\n\n' "'newdep'" >&2; exit 1; }
  case "${AVRONE_DATA_DIR:-}" in *lane-*)
    if [ -e "$STUB_DIR/chat_err_text" ]; then
      printf '%s\n' "task: Land TWO accepted." "tool.call a evolve_propose {}" "tool.result a def x(): return 'ACCEPTED (tests pass'" "agent.done a evolve_propose was ACCEPTED, accepted, Accepted" >&2
    fi
    [ -e "$STUB_DIR/chat_crash_lanes" ] && { echo "lane boom" >&2; exit 3; }
    if [ -e "$STUB_DIR/chat_fake_accept" ]; then
      mkdir -p "$AVRONE_DATA_DIR/evolve"
      echo '{"pid": "p-new-ok", "passed": true}' > "$AVRONE_DATA_DIR/evolve/p-new-ok.json"
      echo '{"pid": "p-new-bad", "passed": false}' > "$AVRONE_DATA_DIR/evolve/p-new-bad.json"
      echo '{"id": "loop-1", "iterations": []}' > "$AVRONE_DATA_DIR/evolve/loop-1.json"
      printf '{' > "$AVRONE_DATA_DIR/evolve/torn.json"
    fi;;
  esac
  if [ -e "$STUB_DIR/chat_orphan" ]; then sleep 300 & echo $! >> "$STUB_DIR/orphans"; exec sleep 300; fi
  [ -e "$STUB_DIR/chat_hang" ] && exec sleep 300
  [ -e "$STUB_DIR/chat_noop" ] && { echo "[r1] agent.done       agent-1            nothing to do" >&2; echo OK; exit 0; }
  if [ -e "$STUB_DIR/chat_spoof" ]; then
    for t in $(echo "$AVRONE_TOOLS_ONLY" | tr , ' '); do echo "[r1] tool.result     agent-1            tool.call agent-1 $t {} (cat of an old log)" >&2; done
    echo OK; exit 0
  fi
  if [ -e "$STUB_DIR/chat_smoke_pass" ]; then
    for t in $(echo "$AVRONE_TOOLS_ONLY" | tr , ' '); do echo "tool.call agent-1 $t {}" >&2; done
    echo "OK"; exit 0
  fi;;
esac
case " $* " in *" avrone.cli evolve "*) [ -e "$STUB_DIR/evolve_hang" ] && exec sleep 300;; esac
exec "@PY@" "$@"
'''


def path_without(tmp: Path, *names: str) -> str:
    """A PATH holding symlinks to every executable on the current PATH except `names`."""
    d = tmp / ("path-without-" + "-".join(names)); d.mkdir(exist_ok=True)
    for pdir in os.environ["PATH"].split(":"):
        if not os.path.isdir(pdir):
            continue
        for f in os.listdir(pdir):
            if f in names or (d / f).exists():
                continue
            src = os.path.join(pdir, f)
            if os.access(src, os.X_OK) and not os.path.isdir(src):
                (d / f).symlink_to(src)
    return str(d)


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


@pytest.fixture(scope="session")
def base_repos(tmp_path_factory):
    """Two pristine repo copies: 'patched' (current HEAD) and 'unpatched' (upstream base)."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs the git checkout")
    out = {}
    for name, ref in (("patched", "HEAD"), ("unpatched", BASE_COMMIT)):
        d = tmp_path_factory.mktemp(name) / "repo"
        r = subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(d)], capture_output=True, text=True)
        if r.returncode:
            pytest.skip(f"clone failed: {r.stderr}")
        if _git(d, "checkout", "-q", ref).returncode:
            pytest.skip(f"{ref} not available")
        # always test the CURRENT script + state, even on the old base
        shutil.copytree(ROOT / "scripts", d / "scripts", dirs_exist_ok=True)
        _git(d, "config", "user.email", "t@t"); _git(d, "config", "user.name", "t")
        out[name] = d
    return out


class Env:
    def __init__(self, tmp: Path, repo: Path, drive_name="drive", repo_name="repo"):
        self.tmp = tmp
        self.repo = tmp / repo_name
        shutil.copytree(repo, self.repo, symlinks=True)
        # fake venv: real >=3.11 interpreter, empty activate (no pip install in tests)
        venv = self.repo / ".venv" / "bin"
        shutil.rmtree(self.repo / ".venv", ignore_errors=True)
        venv.mkdir(parents=True)
        (venv / "activate").write_text("")
        (venv / "python").symlink_to(sys.executable)
        self.drive = tmp / drive_name
        self.drive.mkdir()
        self.stub = tmp / "stub"; self.stub.mkdir()
        self.bin = tmp / "bin"; self.bin.mkdir()
        (self.stub / "models").write_text("stub-model\n")
        for name, body in {
            "ollama": STUB_OLLAMA,
            "brew": '#!/bin/sh\necho "brew $*" >> "$STUB_DIR/calls.log"\n[ "$1" = --prefix ] && echo /usr\nexit 0\n',
            "launchctl": "#!/bin/sh\nexit 0\n",
            "python": STUB_PYTHON.replace("@PY@", sys.executable),
        }.items():
            p = self.bin / name; p.write_text(body); p.chmod(0o755)
        self.port = _free_port()

    def flag(self, name, content=""):
        (self.stub / name).write_text(content)

    def env(self, **kw):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AVRONE_", "OLLAMA_"))}
        e.update(PATH=f"{self.bin}:{e['PATH']}", STUB_DIR=str(self.stub), DRIVE=str(self.drive),
                 OLLAMA_HOST=f"127.0.0.1:{self.port}", MODEL="stub-model", ONCE="1", LANES="",
                 SKIP_GATE="1", AUTOPILOT_NO_INSTALL="1", AVRONE_LLM_MODE="mock",
                 SMOKE_TIMEOUT="60", LANE_TIMEOUT="60", CYCLE_SLEEP="0", MIN_FREE_GB="0", MIN_RUN_FREE_GB="0",
                 PYTHONDONTWRITEBYTECODE="1")
        e.update({k: str(v) for k, v in kw.items()})
        return {k: v for k, v in e.items() if v is not None and v != "__unset__"}

    def run(self, timeout=180, **kw):
        r = subprocess.run([BASH, str(self.repo / SCRIPT_REL)], cwd=self.tmp, env=self.env(**kw),
                           capture_output=True, text=True, timeout=timeout)
        self.kill_server()
        return r

    def kill_server(self):
        pidf = self.stub / "serve.pid"
        if pidf.exists():
            try: os.kill(int(pidf.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError): pass
            pidf.unlink()

    @property
    def state(self): return self.drive / "avrone-state"

    def stage(self): return (self.state / "reintro-stage.txt").read_text().strip()

    def calls(self):
        p = self.stub / "calls.log"; return p.read_text() if p.exists() else ""


@pytest.fixture
def env(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"])
    yield e
    e.kill_server()


def out(r): return r.stdout + r.stderr


def sourced(envsh: Path, var: str) -> str:
    """Value of `var` after actually sourcing env.sh under `set -u` (proves it is valid, safe shell)."""
    r = subprocess.run(["bash", "-c", 'set -eu; source "$1"; printf %s "${!2}"', "_", str(envsh), var],
                       capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert r.returncode == 0, r.stderr
    return r.stdout


# ---------------- argument / drive validation ----------------

def test_no_drive_fails_cleanly(env):
    r = env.run(DRIVE="__unset__")
    assert r.returncode == 1 and "set DRIVE" in out(r)


def test_missing_drive(env):
    r = env.run(DRIVE=str(env.tmp / "nope"))
    assert r.returncode == 1 and "not mounted" in out(r)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_readonly_drive(env):
    env.drive.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = env.run()
    finally:
        env.drive.chmod(0o755)
    assert r.returncode == 1 and "not writable" in out(r)


def test_trailing_slash_drive(env):
    r = env.run(DRIVE=str(env.drive) + "/")
    assert r.returncode == 0, out(r)
    assert (env.drive / "avrone-state" / "env.sh").exists()


@pytest.mark.parametrize("bad", [("CYCLES", "abc"), ("CYCLES", "-1"), ("CTX", "big")])
def test_bad_numeric_env(env, bad):
    k, v = bad
    r = env.run(ONCE="0", **{k: v})
    assert r.returncode == 1 and k in out(r)


def test_not_a_checkout(env, tmp_path):
    fake = tmp_path / "fake" / "scripts"; fake.mkdir(parents=True)
    shutil.copy(env.repo / SCRIPT_REL, fake / "x.sh")
    r = subprocess.run([BASH, str(fake / "x.sh")], env=env.env(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "not an AVRONE checkout" in out(r)


# ---------------- happy path ----------------

def test_happy_path_with_spaces_in_drive(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"], drive_name="AI Drive (USB)")
    r = e.run()
    assert r.returncode == 0, out(r)
    envsh = (e.state / "env.sh").read_text()
    assert "AVRONE_PROVIDER=ollama" in envsh and "AVRONE_RECON_ALLOWLIST=127.0.0.1,localhost" in envsh
    assert f"127.0.0.1:{e.port}/v1" in envsh and sourced(e.state / "env.sh", "AVRONE_LLM_MODEL") == "stub-model"
    assert (e.repo / "MERGE_ME.md").exists() and "Reintro stage" in (e.repo / "MERGE_ME.md").read_text()
    assert "=== cycle 1 ===" in out(r) and "done." in out(r)
    assert not (e.state / ".autopilot.lock").exists(), "lock must be released"
    e.kill_server()


def test_lanes_run_in_mock_mode_and_report(env):
    r = env.run(LANES="tools robust")
    assert r.returncode == 0, out(r)
    assert "lane tools start" in out(r) and "lane robust start" in out(r)
    assert "lane tools rc=0" in out(r)
    assert "lane-tools" in (env.repo / "MERGE_ME.md").read_text()


def test_resume_uses_drive_state_not_repo_seed(env):
    env.state.mkdir(parents=True)
    shutil.copy(env.repo / "scripts/avrone-state/reintro-plan.json", env.state)
    (env.state / "reintro-stage.txt").write_text("8\n")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "stage=8/9" in out(r) and "reintro stage 8/9" in out(r)


def test_full_stage_reports_complete(env):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text("9")
    r = env.run()
    assert r.returncode == 0 and "reintro complete: 9/9, all 75 tools active" in out(r)


# ---------------- corrupted state ----------------

@pytest.mark.parametrize("content", ["", "abc", "7.5", "-3", "  \n"])
def test_corrupt_stage_is_reseeded(env, content):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text(content)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert env.stage() == "7"


def test_stage_beyond_plan_is_clamped(env):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text("99")
    r = env.run()
    assert r.returncode == 0 and "clamping" in out(r) and env.stage() == "9"


@pytest.mark.parametrize("plan", ["{not json", '{"core": [], "batches": []}', '{"core": ["a"]}', "[]"])
def test_corrupt_plan_is_reseeded(env, plan):
    env.state.mkdir(parents=True); (env.state / "reintro-plan.json").write_text(plan)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "plan corrupt" in out(r)
    assert list(env.state.glob("reintro-plan.json.bad-*")), "corrupt plan kept for forensics"


def test_corrupt_mcp_json_is_replaced(env):
    env.state.mkdir(parents=True); (env.state / "mcp.json").write_text("{oops")
    assert env.run().returncode == 0
    assert (env.state / "mcp.json").read_text().strip() == '{"servers":{}}'


# ---------------- repo hygiene + patches ----------------

def test_patches_apply_once_on_upstream_base(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    r = e.run()
    assert r.returncode == 0, out(r)
    assert "applying carried patches" in out(r)
    assert "def coerce_args" in (e.repo / "avrone/trinity.py").read_text()
    e.kill_server()
    r2 = e.run()
    assert r2.returncode == 0 and "applying carried patches" not in out(r2), "must be idempotent"


def test_patch_conflict_restores_tree(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    b = e.repo / "avrone/bootstrap.py"
    lines = b.read_text().splitlines(True)
    b.write_text("".join(lines[:195]) + "# conflicting local edit\n" * 3 + "".join(lines[210:]))
    _git(e.repo, "commit", "-qam", "conflict")
    r = e.run()
    assert r.returncode == 1 and "patch apply failed" in out(r)
    assert not (e.repo / ".git/rebase-apply").exists(), "git am must be aborted"
    e.kill_server()


def test_leftover_git_am_is_aborted(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    (e.repo / ".git/rebase-apply").mkdir()
    r = e.run()
    assert "leftover git am" in out(r)
    e.kill_server()


def test_runtime_agents_dir_kept_but_git_ignored(env):
    (env.repo / "agents").mkdir(); (env.repo / "agents/x.txt").write_text("x")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert (env.repo / "agents/x.txt").exists(), "agent homes are runtime state, never deleted"
    st = _git(env.repo, "status", "--short").stdout
    assert "agents" not in st and "MERGE_ME" not in st, st
    r2 = env.run()
    assert (env.repo / ".git/info/exclude").read_text().count("/agents/") == 1, "exclude must be idempotent"


def test_tracked_agents_kept(env):
    (env.repo / "agents").mkdir(); (env.repo / "agents/keep.py").write_text("x=1\n")
    _git(env.repo, "add", "agents"); _git(env.repo, "commit", "-qm", "agents")
    r = env.run()
    assert r.returncode == 0 and (env.repo / "agents/keep.py").exists() and "tracked in git" in out(r)


def test_ignored_agents_kept(env):
    (env.repo / "agents").mkdir(); (env.repo / "agents/data.bin").write_text("x")
    with open(env.repo / ".gitignore", "a") as f: f.write("\nagents/\n")
    r = env.run()
    assert r.returncode == 0 and (env.repo / "agents/data.bin").exists()


# ---------------- ollama / model ----------------

def test_ollama_fails_to_start(env):
    env.flag("serve_fail")
    r = env.run()
    assert r.returncode == 1 and "ollama did not start" in out(r)


def test_model_pulled_when_missing(env):
    (env.stub / "models").write_text("")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ollama pull stub-model" in env.calls()


def test_pull_retries_then_fails(env):
    (env.stub / "models").write_text(""); env.flag("pull_fail")
    r = env.run(timeout=240)
    assert r.returncode == 1 and "after 3 attempts" in out(r)
    assert env.calls().count("ollama pull") == 3


def test_low_disk_blocks_pull(env):
    (env.stub / "models").write_text("")
    r = env.run(MIN_FREE_GB="999999999")
    assert r.returncode == 1 and "not enough space" in out(r)
    assert "ollama pull" not in env.calls()


def test_disk_check_skipped_when_model_present(env):
    assert env.run(MIN_FREE_GB="999999999").returncode == 0


def test_silent_model_warmup_fails(env):
    env.flag("run_silent")
    r = env.run()
    assert r.returncode == 1 and "no output on warm-up" in out(r)


def test_ollama_dies_mid_run_is_restarted(env):
    env.flag("max_req", "3")   # stub server exits after 3 health checks
    r = env.run(CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "ollama is down — restarting" in out(r)
    assert out(r).count("=== cycle") == 2


def test_ollama_restart_storm_gives_up(env):
    env.flag("max_req", "1")
    r = env.run(CYCLES="0", ONCE="0", timeout=240)
    assert r.returncode == 1 and "keeps dying" in out(r)


def test_already_running_ollama_is_reused(env):
    env.flag("max_req", "0")
    srv = subprocess.Popen([str(env.bin / "ollama"), "serve"], env=env.env())
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", env.port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        r = subprocess.run([BASH, str(env.repo / SCRIPT_REL)], env=env.env(), capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, out(r)
        assert "starting ollama serve" not in out(r)
        assert srv.poll() is None, "must not kill an ollama it did not start"
    finally:
        srv.kill()


def test_no_brew_needed_when_everything_installed(env):
    (env.bin / "brew").unlink()
    assert env.run().returncode == 0


def test_no_brew_and_no_ollama(env, tmp_path):
    (env.bin / "brew").unlink(); (env.bin / "ollama").unlink()
    path = ":".join(p for p in os.environ["PATH"].split(":") if not (Path(p) / "ollama").exists() and not (Path(p) / "brew").exists())
    r = env.run(PATH=f"{env.bin}:{path}")
    assert r.returncode == 1 and "Homebrew is required" in out(r)


# ---------------- gate ----------------

def test_red_gate_stops_before_any_cycle(env):
    r = env.run(SKIP_GATE="0", GATE_CMD="echo '1 failed'; exit 1")
    assert r.returncode == 1 and "baseline RED" in out(r) and "=== cycle" not in out(r)


def test_green_gate_logged(env):
    r = env.run(SKIP_GATE="0", GATE_CMD="echo '5 passed in 0.1s'")
    assert r.returncode == 0 and "baseline: 5 passed" in out(r)


# ---------------- concurrency + signals ----------------

def test_second_instance_refused(env):
    (env.state / ".autopilot.lock").mkdir(parents=True)
    (env.state / ".autopilot.lock/pid").write_text(str(os.getpid()))   # alive
    r = env.run()
    assert r.returncode == 2 and "already running" in out(r)
    assert (env.state / ".autopilot.lock").exists(), "must not steal a live lock"


def test_stale_lock_is_recovered(env):
    p = subprocess.Popen(["true"]); p.wait()
    (env.state / ".autopilot.lock").mkdir(parents=True)
    (env.state / ".autopilot.lock/pid").write_text(str(p.pid))   # dead
    r = env.run()
    assert r.returncode == 0 and "stale lock" in out(r)


def test_two_concurrent_runs_one_wins(env):
    e = env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="2")
    a = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.05)
    b = subprocess.run([BASH, str(env.repo / SCRIPT_REL)], env=e, capture_output=True, text=True, timeout=60)
    a.send_signal(signal.SIGTERM)
    try: a.communicate(timeout=30)
    except subprocess.TimeoutExpired: a.kill(); raise
    assert b.returncode == 2 or a.returncode == 2


def test_sigterm_cleans_up_lock_and_ollama(env):
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="60"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log = ""
    deadline = time.time() + 90
    while time.time() < deadline and "=== cycle 1 ===" not in log:
        time.sleep(0.5)
        runs = list((env.drive / "avrone-runs").glob("*/autopilot.log"))
        log = runs[0].read_text() if runs else ""
    assert "=== cycle 1 ===" in log
    time.sleep(1)
    p.send_signal(signal.SIGTERM)
    t0 = time.time(); p.communicate(timeout=20)
    assert time.time() - t0 < 15, "must not wait out the 60s cycle sleep"
    assert p.returncode == 143
    assert not (env.state / ".autopilot.lock").exists()
    pid = int((env.stub / "serve.pid").read_text())
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_rerun_after_success_is_clean(env):
    assert env.run().returncode == 0
    r = env.run()
    assert r.returncode == 0, out(r)
    assert len(list((env.drive / "avrone-runs").iterdir())) == 2, "each run gets its own results dir"


# =====================================================================================
# Round 2: deeper edges
# =====================================================================================
import importlib.util

_spec = importlib.util.spec_from_file_location("smoke_judge", ROOT / "scripts/avrone-state/smoke_judge.py")
smoke_judge = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(smoke_judge)


def _judge(tmp_path, log: str | bytes | None, nxt: str, stage="7"):
    errf = tmp_path / "smoke.err"
    if log is not None:
        errf.write_bytes(log if isinstance(log, bytes) else log.encode())
    sf = tmp_path / "stage.txt"; sf.write_text(stage)
    r = subprocess.run([sys.executable, str(ROOT / "scripts/avrone-state/smoke_judge.py"), str(errf), nxt, str(sf), stage],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout, sf.read_text(), (tmp_path / "stage.txt.bak")


def _calls(*tools): return "\n".join(f"12:00 tool.call agent-1 {t} {{}}" for t in tools)


class TestSmokeJudge:
    def test_all_called_advances_and_backs_up(self, tmp_path):
        out_, st, bak = _judge(tmp_path, _calls("a", "b", "c"), "a,b,c")
        assert "ADVANCE" in out_ and st == "8" and bak.read_text() == "8"
        assert not list(tmp_path.glob("*.tmp.*")), "atomic write leaves no temp files"

    def test_exactly_sixty_percent_advances(self, tmp_path):
        assert "ADVANCE" in _judge(tmp_path, _calls("a", "b", "c"), "a,b,c,d,e")[0]

    def test_below_sixty_percent_holds(self, tmp_path):
        out_, st, bak = _judge(tmp_path, _calls("a", "b"), "a,b,c,d,e")
        assert "HOLD" in out_ and st == "7" and not bak.exists()

    def test_too_many_malformed_holds(self, tmp_path):
        log = _calls("a", "b") + "\naction.malformed\n" * 4
        assert "HOLD" in _judge(tmp_path, log, "a,b")[0]

    def test_unknown_tool_counts_as_bad(self, tmp_path):
        log = _calls("a") + "\nError: Unknown tool 'x'\n" * 4
        assert "malformed/unknown=4" in _judge(tmp_path, log, "a")[0]

    def test_three_bad_still_advances(self, tmp_path):
        log = _calls("a") + "\naction.malformed" * 3
        assert "ADVANCE" in _judge(tmp_path, log, "a")[0]

    def test_missing_log_holds(self, tmp_path):
        out_, st, _ = _judge(tmp_path, None, "a,b")
        assert "HOLD" in out_ and st == "7"

    def test_binary_garbage_log_holds(self, tmp_path):
        assert "HOLD" in _judge(tmp_path, os.urandom(200_000), "a,b")[0]

    def test_empty_next_is_noop(self, tmp_path):
        out_, st, _ = _judge(tmp_path, _calls("a"), "-")
        assert "HOLD" in out_ and st == "7"

    def test_duplicate_next_names_counted_once(self, tmp_path):
        assert "1/1 exercised" in _judge(tmp_path, _calls("a"), "a,a,a")[0]

    def test_prefix_names_do_not_match(self, tmp_path):
        out_ = _judge(tmp_path, _calls("bridge_post_extra", "spawnx"), "bridge_post,spawn")[0]
        assert "0/2" in out_ and "HOLD" in out_

    def test_non_integer_stage_never_crashes(self, tmp_path):
        out_, st, _ = _judge(tmp_path, _calls("a"), "a", stage="x")
        assert "ADVANCE" in out_ and st == "x"

    def test_wrong_argc_is_hold(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/avrone-state/smoke_judge.py")], capture_output=True, text=True)
        assert r.returncode == 0 and "HOLD" in r.stdout

    def test_judge_function_direct(self):
        assert smoke_judge.judge("", []) == (False, [], 0)
        ok, hit, bad = smoke_judge.judge(_calls("x", "y"), ["x", "y", " "])
        assert ok and hit == ["x", "y"] and bad == 0


# ---------------- progression across cycles ----------------

def test_smoke_pass_advances_stage_end_to_end(env):
    env.flag("chat_smoke_pass")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ADVANCE" in out(r) and env.stage() == "8"
    assert (env.state / "reintro-stage.txt.bak").read_text() == "8"


def test_multi_cycle_reaches_full_registry(env):
    env.flag("chat_smoke_pass")
    r = env.run(CYCLES="3", ONCE="0")
    assert r.returncode == 0, out(r)
    assert env.stage() == "9" and "reintro complete: 9/9, all 75 tools active" in out(r)


def test_stage_deleted_mid_run_restored_from_backup(env):
    env.flag("chat_smoke_pass"); env.flag("chat_delete_stage")
    r = env.run(CYCLES="2", ONCE="0", LANES="tools")
    assert r.returncode == 0, out(r)
    assert "stage file vanished — restoring 8 from backup" in out(r)


def test_stage_deleted_without_backup_falls_back_to_repo(env):
    env.flag("chat_delete_stage")
    r = env.run(CYCLES="2", ONCE="0", LANES="tools")
    assert r.returncode == 0 and "restoring 7 from repo" in out(r)


def test_plan_corrupted_mid_run_is_reseeded(env):
    env.flag("chat_corrupt_plan")
    r = env.run(CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "plan corrupt — reseeding" in out(r) and out(r).count("=== cycle") == 2


@pytest.mark.parametrize("content,expect", [("0000007", "7"), ("99999999999999999999", "7"), ("1e3", "7"), ("७", "7"), ("8\r\n", "8")])
def test_more_stage_encodings(env, content, expect):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text(content)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert str(int(env.stage())) == expect


@pytest.mark.parametrize("plan", ['{"core":["a","a"],"batches":[["b"]]}', '{"core":["a"],"batches":[["a"]]}',
                                  '{"core":["Bad Name"],"batches":[["b"]]}', '{"core":["a"],"batches":[[]]}',
                                  '{"core":["a"],"batches":[["b",3]]}', '{"core":["' + "x" * 65 + '"],"batches":[["b"]]}'])
def test_semantically_bad_plans_reseeded(env, plan):
    env.state.mkdir(parents=True); (env.state / "reintro-plan.json").write_text(plan)
    r = env.run()
    assert r.returncode == 0 and "plan corrupt" in out(r)


# ---------------- lanes misbehaving ----------------

def test_hanging_lane_times_out_and_loop_continues(env):
    env.flag("chat_hang")
    t0 = time.time()
    r = env.run(LANES="tools robust", LANE_TIMEOUT="2", SMOKE_TIMEOUT="2")
    assert r.returncode == 0, out(r)
    assert "lane tools rc=124 (timed out)" in out(r) and "lane robust rc=124 (timed out)" in out(r)
    assert "HOLD" in out(r) and (env.repo / "MERGE_ME.md").exists()
    assert time.time() - t0 < 60


def test_crashing_lane_does_not_stop_the_loop(env):
    env.flag("chat_crash")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert out(r).count("lane tools rc=3") == 2 and "done." in out(r)


def test_perl_timeout_fallback_when_no_timeout_binary(env, tmp_path):
    if shutil.which("perl") is None:
        pytest.skip("no perl")
    env.flag("chat_hang")
    p = path_without(tmp_path, "timeout", "gtimeout", "brew")
    r = env.run(PATH=f"{env.bin}:{p}", LANES="tools", LANE_TIMEOUT="2", SMOKE_TIMEOUT="2")
    assert r.returncode == 0, out(r)
    assert "lane tools rc=124" in out(r)


# ---------------- git / working tree ----------------

def test_dirty_tree_blocks_patching_and_preserves_edits(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    t = e.repo / "avrone/trinity.py"
    t.write_text(t.read_text() + "\n# MY PRECIOUS UNCOMMITTED EDIT\n")
    r = e.run()
    assert r.returncode == 1 and "uncommitted changes" in out(r)
    assert "MY PRECIOUS UNCOMMITTED EDIT" in t.read_text()
    assert "def coerce_args" not in t.read_text()
    e.kill_server()


def test_dirty_tree_fine_when_no_patch_needed(env):
    t = env.repo / "README.md"; t.write_text(t.read_text() + "\nlocal note\n")
    r = env.run()
    assert r.returncode == 0 and "local note" in t.read_text()


def test_git_missing(env, tmp_path):
    r = env.run(PATH=f"{env.bin}:{path_without(tmp_path, 'git')}",
                AUTOPILOT_BREW_PREFIXES=str(tmp_path / "no-brew"))
    assert r.returncode == 1 and "git is required" in out(r)


# ---------------- paths ----------------

def test_repo_path_with_spaces_and_unicode(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"], drive_name="Drïve ü 2", repo_name="my repö dir")
    r = e.run(LANES="tools")
    assert r.returncode == 0, out(r)
    assert sourced(e.state / "env.sh", "AVRONE_REPO_DIR") == str(e.repo)
    assert "lane tools rc=0" in out(r)
    e.kill_server()


def test_symlinked_drive(env, tmp_path):
    link = tmp_path / "Volumes-AI"; link.symlink_to(env.drive)
    r = env.run(DRIVE=str(link))
    assert r.returncode == 0, out(r)
    assert (env.drive / "avrone-state" / "env.sh").exists()


# ---------------- config parsing ----------------

@pytest.mark.parametrize("ctx,ok", [("1024", False), ("99999999", False), ("2048", True), ("0", False)])
def test_ctx_bounds(env, ctx, ok):
    r = env.run(CTX=ctx)
    assert (r.returncode == 0) == ok, out(r)


@pytest.mark.parametrize("fmt", ["http://127.0.0.1:{p}", "http://127.0.0.1:{p}/", "127.0.0.1:{p}/v1"])
def test_ollama_host_formats(env, fmt):
    r = env.run(OLLAMA_HOST=fmt.format(p=env.port))
    assert r.returncode == 0, out(r)
    assert sourced(env.state / "env.sh", "AVRONE_LLM_BASE_URL") == f"http://127.0.0.1:{env.port}/v1"


def test_model_prefix_is_not_a_match(env):
    (env.stub / "models").write_text("stub-model-big\n")
    r = env.run()
    assert r.returncode == 0 and "ollama pull stub-model" in env.calls()


def test_model_with_regex_chars_matched_literally(env):
    (env.stub / "models").write_text("a.b+c:1\n")
    r = env.run(MODEL="a.b+c:1")
    assert r.returncode == 0 and "ollama pull" not in env.calls()


def test_low_ram_warns_for_big_model(env):
    (env.stub / "models").write_text("gpt-oss:120b\n")
    r = env.run(MODEL="gpt-oss:120b", AUTOPILOT_RAM_GB="16")
    assert r.returncode == 0 and "wants ~72GB RAM" in out(r)


def test_enough_ram_no_warning(env):
    (env.stub / "models").write_text("gpt-oss:120b\n")
    r = env.run(MODEL="gpt-oss:120b", AUTOPILOT_RAM_GB="128")
    assert r.returncode == 0 and "RAM" not in out(r)


# ---------------- signals ----------------

def _start_and_wait_cycle(env, **kw):
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="60", **kw),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 90; log = ""
    while time.time() < deadline and "summary written" not in log:
        time.sleep(0.3)
        runs = list((env.drive / "avrone-runs").glob("*/autopilot.log"))
        log = runs[0].read_text() if runs else ""
    assert "summary written" in log
    return p


def test_sigint_exits_130_and_cleans(env):
    p = _start_and_wait_cycle(env)
    p.send_signal(signal.SIGINT); p.communicate(timeout=20)
    assert p.returncode == 130 and not (env.state / ".autopilot.lock").exists()


def test_sighup_is_survived(env):
    p = _start_and_wait_cycle(env)
    p.send_signal(signal.SIGHUP); time.sleep(1.5)
    assert p.poll() is None, "terminal closing must not kill the autopilot"
    p.send_signal(signal.SIGTERM); o, _ = p.communicate(timeout=20)
    assert p.returncode == 143 and "SIGHUP" in o


# =====================================================================================
# Round 3: hostile environment
# =====================================================================================

# ---------------- injection / input validation ----------------

@pytest.mark.parametrize("model", ['x"; touch /tmp/pwned_avrone; "', "a b", "$(id)", "m`id`", "a;b", "x\ny"])
def test_model_injection_rejected(env, model):
    Path("/tmp/pwned_avrone").unlink(missing_ok=True)
    r = env.run(MODEL=model)
    assert r.returncode == 1 and ("illegal characters" in out(r) or "MODEL" in out(r)), out(r)
    assert not Path("/tmp/pwned_avrone").exists()
    assert not (env.state / "env.sh").exists()


@pytest.mark.parametrize("model", ["gpt-oss:120b", "qwen3-coder-next:q4_K_M", "hf.co/org/Model-GGUF:Q4_K_M", "a.b+c:1"])
def test_legit_model_names_accepted(env, model):
    (env.stub / "models").write_text(model + "\n")
    r = env.run(MODEL=model, AUTOPILOT_RAM_GB="512")
    assert r.returncode == 0, out(r)
    assert sourced(env.state / "env.sh", "AVRONE_LLM_MODEL") == model


@pytest.mark.parametrize("lanes", ["tools; rm -rf /", "bogus", "tools $(id)"])
def test_bad_lanes_rejected(env, lanes):
    r = env.run(LANES=lanes)
    assert r.returncode == 1 and "unknown lane" in out(r)


@pytest.mark.parametrize("host", ['127.0.0.1:1"; id; "', "h ost:1", "$(id):1"])
def test_ollama_host_injection_rejected(env, host):
    r = env.run(OLLAMA_HOST=host)
    assert r.returncode == 1 and "illegal characters" in out(r)


def test_drive_root_rejected(env):
    r = env.run(DRIVE="/")
    assert r.returncode == 1 and ("filesystem root" in out(r) or "not writable" in out(r))


def test_drive_inside_repo_rejected(env):
    d = env.repo / "scratch-drive"; d.mkdir()
    r = env.run(DRIVE=str(d))
    assert r.returncode == 1 and "inside the repo" in out(r)
    assert not (d / "avrone-state").exists()


def test_drive_symlink_into_repo_rejected(env, tmp_path):
    link = tmp_path / "sneaky"; link.symlink_to(env.repo)
    r = env.run(DRIVE=str(link))
    assert r.returncode == 1 and "inside the repo" in out(r)


def test_drive_is_home_rejected(env):
    r = env.run(HOME=str(env.drive))
    assert r.returncode == 1 and "home folder" in out(r)


def test_root_refused_unless_allowed(env):
    (env.bin / "id").write_text('#!/bin/sh\n[ "$1" = -u ] && echo 0 || /usr/bin/id "$@"\n'); (env.bin / "id").chmod(0o755)
    r = env.run()
    assert r.returncode == 1 and "do not run as root" in out(r)
    r = env.run(ALLOW_ROOT="1")
    assert r.returncode == 0, out(r)


# ---------------- the drive misbehaving ----------------

def test_drive_unplugged_mid_run_stops_cleanly(env):
    env.flag("chat_unplug")
    r = env.run(LANES="tools", CYCLES="3", ONCE="0", timeout=120)
    assert r.returncode == 1 and "disappeared or was swapped" in out(r)
    assert not env.drive.exists(), "must not recreate the unplugged mount point"
    assert out(r).count("=== cycle") == 1


def test_drive_swapped_mid_run_detected(env):
    # another drive with the same mount name: token no longer matches
    env.flag("chat_smoke_pass")
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="3"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 90
    while time.time() < deadline and not (env.state / ".autopilot.lock" / "token").exists():
        time.sleep(0.2)
    time.sleep(1)
    (env.state / ".autopilot.lock" / "token").write_text("someone-elses-drive")
    o, _ = p.communicate(timeout=90)
    env.kill_server()
    assert p.returncode == 1 and "disappeared or was swapped" in o


def test_state_goes_readonly_while_advancing(env):
    env.flag("chat_smoke_pass"); env.flag("chat_readonly_state")
    try:
        r = env.run()
    finally:
        os.chmod(env.state, 0o755)
    assert r.returncode == 0, out(r)
    assert "could not save stage" in out(r) and env.stage() == "7"


def test_stage_path_is_a_directory(env):
    (env.state / "reintro-stage.txt").mkdir(parents=True)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "directory or unreadable" in out(r) and env.stage() == "7"


def test_stage_file_unreadable(env):
    env.state.mkdir(parents=True); f = env.state / "reintro-stage.txt"; f.write_text("8"); f.chmod(0)
    try:
        r = env.run()
    finally:
        for g in env.state.glob("reintro-stage.txt*"):
            g.chmod(0o644)
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    assert r.returncode == 0, out(r)
    assert "unreadable" in out(r) and env.stage() in {"7", "8"}


def test_plan_path_is_a_directory(env):
    (env.state / "reintro-plan.json").mkdir(parents=True)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert (env.state / "reintro-plan.json").is_file()


def test_stale_tmp_files_from_killed_write_ignored(env):
    env.state.mkdir(parents=True)
    (env.state / "reintro-stage.txt").write_text("8")
    (env.state / "reintro-stage.txt.tmp.99999").write_text("garbage")
    r = env.run()
    assert r.returncode == 0 and env.stage() == "8"


# ---------------- ollama misbehaving ----------------

def test_our_own_hung_ollama_is_killed_and_restarted(env):
    env.flag("serve_hang_after", "2")
    r = env.run(CYCLES="2", ONCE="0", timeout=150)
    assert r.returncode == 0, out(r)
    assert "ollama is down — restarting" in out(r)


def test_foreign_hung_listener_detected_not_waited_forever(env):
    import socket
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", env.port)); srv.listen(16)   # accepts connections, never answers
    try:
        t0 = time.time()
        r = env.run(timeout=120)
    finally:
        srv.close()
    assert r.returncode == 1 and "does not answer (hung ollama?)" in out(r), out(r)
    assert time.time() - t0 < 60


def test_warmup_hang_times_out(env):
    env.flag("run_hang")
    t0 = time.time()
    r = env.run(WARM_TIMEOUT="3")
    assert r.returncode == 1 and "no output on warm-up within 3s" in out(r)
    assert time.time() - t0 < 60


def test_model_deleted_mid_run_is_repulled(env):
    env.flag("chat_remove_model")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "ollama pull stub-model" in env.calls() and "pulling stub-model" in out(r)


def test_model_deleted_and_disk_full_mid_run(env):
    env.flag("chat_remove_model")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0", MIN_FREE_GB="999999")
    assert r.returncode == 1 and "not enough space" in out(r)


# ---------------- floods ----------------

def test_output_flood_from_lanes_is_contained(env):
    env.flag("chat_flood")
    r = env.run(LANES="tools robust")
    assert r.returncode == 0, out(r)
    assert len(r.stdout) < 200_000, "lane output must go to files, not the terminal"
    run = next((env.drive / "avrone-runs").iterdir())
    assert (run / "tools.txt").stat().st_size >= 3_000_000



# =====================================================================================
# Round 4: slow-burn failures, process hygiene, stripped environments
# =====================================================================================

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # zombies count as dead
        return "Z" not in open(f"/proc/{pid}/stat").read().split(")")[-1].split()[0]
    except OSError:
        return True


def _orphans(env):
    f = env.stub / "orphans"
    return [int(x) for x in f.read_text().split()] if f.exists() else []


def _reap(pids):
    for p in pids:
        try: os.kill(p, signal.SIGKILL)
        except ProcessLookupError: pass


# ---------------- process hygiene ----------------

@pytest.mark.parametrize("impl", ["native", "perl"])
def test_lane_timeout_kills_grandchildren(env, impl):
    if impl == "perl" and shutil.which("perl") is None:
        pytest.skip("no perl")
    env.flag("chat_orphan")
    kw = {"AUTOPILOT_TIMEOUT_IMPL": "perl"} if impl == "perl" else {}
    r = env.run(LANES="tools", LANE_TIMEOUT="2", SMOKE_TIMEOUT="2", **kw)
    pids = _orphans(env)
    try:
        assert r.returncode == 0, out(r)
        assert pids, "stub never spawned its grandchild"
        time.sleep(3)
        leaked = [p for p in pids if _alive(p)]
        assert not leaked, f"{impl}: grandchildren survived the timeout: {leaked}"
    finally:
        _reap(pids)


def test_sigterm_kills_whole_tree(env):
    env.flag("chat_orphan")
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(LANES="tools"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 60
    while time.time() < deadline and len(_orphans(env)) < 1:
        time.sleep(0.2)
    time.sleep(1)
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=30)
    env.kill_server()
    pids = _orphans(env)
    try:
        assert pids
        time.sleep(3)
        assert not [x for x in pids if _alive(x)], "orphans left after SIGTERM"
        assert not (env.state / ".autopilot.lock").exists()
        assert not (env.repo / ".git" / "avrone-autopilot.lock").exists()
    finally:
        _reap(pids)


# ---------------- two autopilots, one repo ----------------

def test_second_autopilot_same_repo_other_drive_refused(env, tmp_path):
    env.flag("chat_hang")
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(LANES="tools", LANE_TIMEOUT="30", SMOKE_TIMEOUT="30"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lock = env.repo / ".git" / "avrone-autopilot.lock" / "pid"
    deadline = time.time() + 60
    while time.time() < deadline and not lock.exists():
        time.sleep(0.2)
    other = tmp_path / "drive2"; other.mkdir()
    r = subprocess.run([BASH, str(env.repo / SCRIPT_REL)], env=env.env(DRIVE=str(other)),
                       capture_output=True, text=True, timeout=60)
    p.send_signal(signal.SIGTERM); p.communicate(timeout=60); env.kill_server()
    assert r.returncode == 2 and "already using this repo" in out(r), out(r)


def test_stale_repo_lock_recovered(env):
    lk = env.repo / ".git" / "avrone-autopilot.lock"; lk.mkdir()
    (lk / "pid").write_text("999999")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert not lk.exists()


# ---------------- git crash leftovers ----------------

def test_stale_index_lock_removed(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    (e.repo / ".git" / "index.lock").write_text("")
    r = e.run()
    assert r.returncode == 0, out(r)
    assert "stale git index.lock" in out(r) and "def coerce_args" in (e.repo / "avrone/trinity.py").read_text()


def test_detached_head_still_works(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    _git(e.repo, "checkout", "-q", "--detach")
    r = e.run()
    assert r.returncode == 0, out(r)
    assert "def coerce_args" in (e.repo / "avrone/trinity.py").read_text()


# ---------------- stripped / odd environments ----------------

def test_home_unset(env):
    r = env.run(HOME="__unset__")
    assert r.returncode == 0, out(r)
    assert "unbound variable" not in out(r)


def test_c_locale(env):
    r = env.run(LC_ALL="C", LANG="C")
    assert r.returncode == 0, out(r)


def test_bad_tmpdir(env):
    r = env.run(TMPDIR="/nonexistent/tmp")
    assert r.returncode == 0, out(r)


@pytest.mark.parametrize("name", ['we$ird `x` "q"', "it's $(id) here", "a\\b"])
def test_hostile_repo_and_drive_names_source_safely(tmp_path, base_repos, name):
    e = Env(tmp_path, base_repos["patched"], drive_name="drv " + name, repo_name="repo " + name)
    Path("/tmp/pwned_avrone").unlink(missing_ok=True)
    r = e.run()
    assert r.returncode == 0, out(r)
    envsh = e.state / "env.sh"
    assert sourced(envsh, "AVRONE_REPO_DIR") == str(e.repo)
    assert sourced(envsh, "AVRONE_MCP_CONFIG") == str(e.state / "mcp.json")
    assert not Path("/tmp/pwned_avrone").exists()


# ---------------- long-running hygiene ----------------

def test_old_runs_pruned(env):
    runs = env.drive / "avrone-runs"; runs.mkdir(parents=True)
    for i in range(8):
        (runs / f"2025010{i % 10}-00000{i}-1").mkdir()
    r = env.run(KEEP_RUNS="3")
    assert r.returncode == 0, out(r)
    left = sorted(p.name for p in runs.iterdir())
    assert len(left) == 3 and "pruned 6 old run folder" in out(r)
    assert any(n.startswith("20") and n > "2025" for n in left)


def test_keep_runs_zero_keeps_everything(env):
    runs = env.drive / "avrone-runs"; runs.mkdir(parents=True)
    for i in range(5):
        (runs / f"old-{i}").mkdir()
    r = env.run(KEEP_RUNS="0")
    assert r.returncode == 0 and len(list(runs.iterdir())) == 6


def test_bad_keep_runs(env):
    r = env.run(KEEP_RUNS="lots")
    assert r.returncode == 1 and "KEEP_RUNS" in out(r)


def test_drive_nearly_full_stops_before_work(env):
    r = env.run(MIN_RUN_FREE_GB="999999", LANES="tools")
    assert r.returncode == 1 and "nearly full" in out(r)
    assert "lane tools start" not in out(r)
    assert env.stage() == "7", "state must survive"


def test_many_cycles_stay_stable(env):
    r = env.run(LANES="tools", CYCLES="5", ONCE="0", timeout=300)
    assert r.returncode == 0, out(r)
    assert out(r).count("=== cycle") == 5 and "WARN: ollama is down" not in out(r)
    assert len(list((env.drive / "avrone-runs").iterdir())) == 1


@pytest.mark.parametrize("where", ["gate", "smoke"])
def test_sigterm_during_long_foreground_step_is_immediate(env, where):
    # bash defers traps until a foreground child exits; long steps must stay interruptible
    kw = {"SKIP_GATE": "__unset__", "GATE_CMD": "sleep 300"} if where == "gate" else {"SMOKE_TIMEOUT": "300"}
    if where == "smoke":
        env.flag("chat_hang")
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(**kw),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    marker = "baseline gate" if where == "gate" else "smoke-testing"
    buf, deadline = "", time.time() + 60
    while time.time() < deadline and marker not in buf:
        line = p.stdout.readline()
        if not line: break
        buf += line
    time.sleep(1)
    t0 = time.time()
    p.send_signal(signal.SIGTERM)
    o, _ = p.communicate(timeout=30)
    env.kill_server()
    assert time.time() - t0 < 10
    logs = "".join(f.read_text() for f in (env.drive / "avrone-runs").glob("*/autopilot.log"))
    assert p.returncode == 143 and "terminated" in logs



# =====================================================================================
# Round 5: how it is launched, symlink tricks, garbage fuzzing, signals during waits
# =====================================================================================

def test_suite_bash_version_reported():
    v = subprocess.run([BASH, "-c", "echo $BASH_VERSION"], capture_output=True, text=True).stdout.strip()
    print("autopilot suite running under bash", v)
    assert v


# ---------------- how it is launched ----------------

def test_launched_with_sh_reexecs_bash(env):
    sh = shutil.which("dash") or shutil.which("sh")
    r = subprocess.run([sh, str(env.repo / SCRIPT_REL)], cwd=env.tmp, env=env.env(), capture_output=True, text=True, timeout=180)
    env.kill_server()
    assert r.returncode == 0, out(r)
    assert "AVRONE autopilot" in out(r)


def test_launched_via_symlink_finds_real_repo(env, tmp_path):
    link_dir = tmp_path / "usr-local-bin"; link_dir.mkdir()
    link = link_dir / "avrone-autopilot"; link.symlink_to(env.repo / SCRIPT_REL)
    r = subprocess.run([BASH, str(link)], cwd=tmp_path, env=env.env(), capture_output=True, text=True, timeout=180)
    env.kill_server()
    assert r.returncode == 0, out(r)
    assert f"repo={env.repo}" in out(r)


def test_launched_via_relative_symlink_chain(env, tmp_path):
    a = tmp_path / "a"; a.mkdir(); b = tmp_path / "b"; b.mkdir()
    (b / "real").symlink_to(os.path.relpath(env.repo / SCRIPT_REL, b))
    (a / "go").symlink_to("../b/real")
    r = subprocess.run([BASH, str(a / "go")], cwd=tmp_path, env=env.env(), capture_output=True, text=True, timeout=180)
    env.kill_server()
    assert r.returncode == 0 and f"repo={env.repo}" in out(r), out(r)


def test_launched_from_inside_another_git_repo(env, tmp_path):
    other = tmp_path / "otherrepo"; other.mkdir(); _git(other, "init", "-q")
    r = subprocess.run([BASH, str(env.repo / SCRIPT_REL)], cwd=other, env=env.env(), capture_output=True, text=True, timeout=180)
    env.kill_server()
    assert r.returncode == 0, out(r)
    assert not (other / "MERGE_ME.md").exists() and (env.repo / "MERGE_ME.md").exists()


def test_stdout_closed_still_runs_and_logs(env):
    r = subprocess.run([BASH, "-c", 'exec >&- 2>&-; "$0" "$1"; echo "rc=$?" > "$2"', BASH,
                        str(env.repo / SCRIPT_REL), str(env.tmp / "rc.txt")],
                       cwd=env.tmp, env=env.env(), timeout=180)
    env.kill_server()
    assert (env.tmp / "rc.txt").read_text().strip() == "rc=0"
    logs = "".join(f.read_text() for f in (env.drive / "avrone-runs").glob("*/autopilot.log"))
    assert "summary written" in logs
    assert "ollama pull" not in env.calls(), "closed stdout must not make the model look missing"


def test_stdin_closed(env):
    r = subprocess.run([BASH, "-c", 'exec <&-; "$0" "$1"', BASH, str(env.repo / SCRIPT_REL)],
                       cwd=env.tmp, env=env.env(), capture_output=True, text=True, timeout=180)
    env.kill_server()
    assert r.returncode == 0, out(r)


# ---------------- symlink tricks on the drive ----------------

@pytest.mark.parametrize("name", ["reintro-stage.txt", "reintro-stage.txt.bak", "reintro-plan.json", "mcp.json", "env.sh"])
def test_state_symlink_never_written_through(env, tmp_path, name):
    victim = tmp_path / "victim.txt"; victim.write_text("PRECIOUS\n")
    env.state.mkdir(parents=True)
    (env.state / name).symlink_to(victim)
    env.flag("chat_smoke_pass")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert victim.read_text() == "PRECIOUS\n", f"wrote through {name} symlink"
    assert not (env.state / name).is_symlink()


def test_lock_path_is_a_regular_file(env):
    env.state.mkdir(parents=True); (env.state / ".autopilot.lock").write_text("junk")
    r = env.run()
    assert r.returncode == 0, out(r)


def test_lock_path_is_symlink_to_dir_elsewhere(env, tmp_path):
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir(); (elsewhere / "keep.txt").write_text("keep")
    env.state.mkdir(parents=True); (env.state / ".autopilot.lock").symlink_to(elsewhere)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert (elsewhere / "keep.txt").read_text() == "keep"


# ---------------- stage format fuzz ----------------

@pytest.mark.parametrize("content,expect", [
    ("08\n", "8"), ("007", "7"), (" 6 \n", "6"), ("6\r\n", "6"), ("\ufeff6", "7"),
    ("\u0661", "7"), ("+3", "7"), ("-1", "7"), ("1e3", "7"), ("0x5", "7"), ("5.0", "7"),
    ("6\n7\n", "7"), ("\x00\x00", "7"), ("9" * 7, "7"), ("", "7"),
])
def test_stage_format_fuzz(env, content, expect):
    env.state.mkdir(parents=True)
    (env.state / "reintro-stage.txt").write_bytes(content.encode("utf-8"))
    r = env.run()
    assert r.returncode == 0, out(r)
    assert env.stage() == expect, (content, env.stage())


def test_huge_stage_file(env):
    env.state.mkdir(parents=True)
    with open(env.state / "reintro-stage.txt", "wb") as f:
        f.write(b"7" * (20 * 1024 * 1024))
    r = env.run()
    assert r.returncode == 0 and env.stage() == "7"


def test_plan_json_recursion_bomb(env):
    env.state.mkdir(parents=True)
    (env.state / "reintro-plan.json").write_text("[" * 200000 + "]" * 200000)
    r = env.run()
    assert r.returncode == 0 and "plan corrupt" in out(r)


def test_binary_garbage_plan(env):
    env.state.mkdir(parents=True)
    (env.state / "reintro-plan.json").write_bytes(os.urandom(4096))
    r = env.run()
    assert r.returncode == 0 and "plan corrupt" in out(r)


# ---------------- smoke judge fuzz (fast, no autopilot run) ----------------

JUDGE = ROOT / "scripts" / "avrone-state" / "smoke_judge.py"


def _fjudge(tmp_path, log_bytes, tools="a,b,c", stage="3"):
    logf = tmp_path / "smoke.err"; logf.write_bytes(log_bytes)
    st = tmp_path / "stage.txt"; st.write_text(stage)
    r = subprocess.run([sys.executable, str(JUDGE), str(logf), tools, str(st), stage],
                       capture_output=True, text=True, timeout=30)
    return r, st.read_text().strip()


def test_judge_random_garbage_never_crashes_or_advances(tmp_path):
    import random
    rnd = random.Random(1337)
    words = [b"tool.call", b"a", b"b", b"c", b"{", b"}", b'"tool"', b":", b"\n", b"\x00", b"\xff",
             b"error", b"unknown tool", b"malformed", b"\xe2\x98\x83", b" ", b"name=", b"args"]
    for i in range(300):
        blob = b"".join(rnd.choice(words) if rnd.random() < .8 else os.urandom(rnd.randint(1, 40))
                        for _ in range(rnd.randint(0, 400)))
        r, st = _fjudge(tmp_path, blob)
        assert r.returncode == 0, (i, r.stderr[-500:])
        assert "Traceback" not in r.stderr, (i, r.stderr[-500:])
        assert st in {"3", "4"}, (i, st)


def test_judge_unicode_tool_names_and_huge_log(tmp_path):
    r, st = _fjudge(tmp_path, ("tool.call ✓ " * 200000).encode(), tools="✓,ß,工具")
    assert r.returncode == 0 and "Traceback" not in r.stderr and st in {"3", "4"}


@pytest.mark.parametrize("tools", ["", ",", ",,,", "a,,b", " a , b "])
def test_judge_odd_tool_lists(tmp_path, tools):
    r, st = _fjudge(tmp_path, b"tool.call a\n", tools=tools)
    assert r.returncode == 0 and "Traceback" not in r.stderr and st in {"3", "4"}


@pytest.mark.parametrize("stage", ["abc", "", "-2", "99999999999999999999"])
def test_judge_bad_current_stage_arg(tmp_path, stage):
    r, st = _fjudge(tmp_path, b"tool.call a\n", stage=stage)
    assert "Traceback" not in r.stderr


# ---------------- signals while waiting on lanes ----------------

@pytest.mark.parametrize("sig", ["SIGHUP", "SIGUSR1"])
def test_signal_during_lanes_does_not_cut_wait_short(env, sig):
    # `wait` returns early when a trapped signal arrives; the pair must still be awaited
    env.flag("chat_sleep", "4")
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(LANES="tools robust", SMOKE_TIMEOUT="60"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    buf, deadline = "", time.time() + 60
    while time.time() < deadline and "lane tools start" not in buf:
        line = p.stdout.readline()
        if not line: break
        buf += line
    time.sleep(0.5)
    for _ in range(3):
        p.send_signal(getattr(signal, sig)); time.sleep(0.3)
    o, _ = p.communicate(timeout=90)
    env.kill_server()
    allout = buf + o
    assert p.returncode == 0, allout
    # both lanes of the first pair must finish before the report
    assert allout.index("lane tools rc=0") < allout.index("summary written")
    assert allout.index("lane robust rc=0") < allout.index("summary written")
    if sig == "SIGUSR1":
        assert "status: pid=" in allout and "phase=lanes" in allout


def test_sigquit_cleans_up(env):
    env.flag("chat_hang")
    p = subprocess.Popen([BASH, str(env.repo / SCRIPT_REL)], env=env.env(LANES="tools", SMOKE_TIMEOUT="60"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 60
    while time.time() < deadline and not (env.state / ".autopilot.lock" / "pid").exists():
        time.sleep(0.2)
    time.sleep(2)
    p.send_signal(signal.SIGQUIT)
    p.communicate(timeout=30); env.kill_server()
    assert p.returncode == 131
    assert not (env.state / ".autopilot.lock").exists()


# ---------------- user edits state by hand mid-run ----------------

def test_user_edits_stage_mid_run_is_honoured(env):
    env.flag("chat_set_stage", "3")
    r = env.run(CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "reintro stage 3/9" in out(r)


# ---------------- corrupt carried patch ----------------

def test_corrupt_patch_file_restores_tree(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    pd = e.repo / "scripts/avrone-state/patches"
    first = sorted(pd.glob("*.patch"))[0]
    first.write_text("From nobody\nSubject: [PATCH] broken\n\n--- a/x\n+++ b/x\n@@ garbage @@\n")
    _git(e.repo, "add", "-A"); _git(e.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "corrupt patch")
    head = _git(e.repo, "rev-parse", "HEAD").stdout.strip()
    r = e.run()
    assert r.returncode == 1 and "patch apply failed" in out(r)
    assert _git(e.repo, "rev-parse", "HEAD").stdout.strip() == head
    assert not (e.repo / ".git" / "rebase-apply").exists()


def test_junk_files_in_patch_dir_ignored(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    pd = e.repo / "scripts/avrone-state/patches"
    (pd / "README.txt").write_text("not a patch"); (pd / ".DS_Store").write_bytes(b"\x00\x01")
    _git(e.repo, "add", "-A"); _git(e.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "junk")
    r = e.run()
    assert r.returncode == 0, out(r)


# ---------------- long paths ----------------

def test_very_long_drive_path(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"])
    deep = e.drive
    for i in range(12):
        deep = deep / ("d" * 40 + str(i))
    deep.mkdir(parents=True)
    r = e.run(DRIVE=str(deep))
    assert r.returncode == 0, out(r)
    assert sourced(deep / "avrone-state" / "env.sh", "AVRONE_MCP_CONFIG") == str(deep / "avrone-state" / "mcp.json")


# =====================================================================================
# Round 6: a real developer Mac + exFAT USB drive + DST + garbage settings
# =====================================================================================

def _gate_out(env):
    return "".join(f.read_text(errors="replace") for f in (env.drive / "avrone-runs").glob("*/baseline.txt"))


def _gitcfg(tmp_path, body):
    f = tmp_path / "global.gitconfig"; f.write_text(body); return str(f)


SIGNING = "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n"


def test_global_gpgsign_does_not_break_patch_apply(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    r = e.run(GIT_CONFIG_GLOBAL=_gitcfg(tmp_path, SIGNING))
    assert r.returncode == 0, out(r)
    assert "applying carried patches" in out(r)


def test_global_gpgsign_does_not_break_child_commits(env, tmp_path):
    # AVRONE's evolve commits run as children; they must inherit the signing override
    gate = ('d=$(mktemp -d) && cd "$d" && git init -q && '
            'git -c user.name=a -c user.email=a@a commit -q --allow-empty -m probe && echo CHILD_COMMIT_OK')
    r = env.run(GIT_CONFIG_GLOBAL=_gitcfg(tmp_path, SIGNING), SKIP_GATE="0", GATE_CMD=gate)
    assert r.returncode == 0, out(r)
    assert "CHILD_COMMIT_OK" in _gate_out(env)


def test_global_failing_hooks_ignored(tmp_path, base_repos):
    hooks = tmp_path / "hooks"; hooks.mkdir()
    for h in ("pre-commit", "commit-msg", "applypatch-msg", "pre-applypatch", "post-applypatch"):
        (hooks / h).write_text("#!/bin/sh\necho HOOK_RAN >&2\nexit 1\n"); (hooks / h).chmod(0o755)
    e = Env(tmp_path, base_repos["unpatched"])
    r = e.run(GIT_CONFIG_GLOBAL=_gitcfg(tmp_path, f"[core]\n\thooksPath = {hooks}\n"))
    assert r.returncode == 0, out(r)


def test_global_autocrlf_true(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    r = e.run(GIT_CONFIG_GLOBAL=_gitcfg(tmp_path, "[core]\n\tautocrlf = true\n"))
    assert r.returncode == 0, out(r)


def test_no_global_git_identity(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    r = e.run(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", EMAIL="__unset__")
    assert r.returncode == 0, out(r)


def test_stray_git_dir_env_is_ignored(env, tmp_path):
    other = tmp_path / "victim-repo"; other.mkdir(); _git(other, "init", "-q")
    before = sorted(p.name for p in (other / ".git").iterdir())
    r = env.run(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), GIT_INDEX_FILE=str(tmp_path / "idx"))
    assert r.returncode == 0, out(r)
    assert sorted(p.name for p in (other / ".git").iterdir()) == before
    assert not (tmp_path / "idx").exists()


@pytest.mark.parametrize("var,val", [
    ("PYTHONHOME", "/nonexistent/python-home"),
    ("PYTHONWARNINGS", "error"),
    ("PYTHONOPTIMIZE", "2"),
    ("PYTEST_ADDOPTS", "--pdb -x"),
    ("PIP_USER", "1"),
    ("PIP_REQUIRE_VIRTUALENV", "1"),
    ("PYTHONSTARTUP", "/nonexistent/startup.py"),
    ("VIRTUAL_ENV", "/nonexistent/venv"),
    ("CONDA_PREFIX", "/nonexistent/conda"),
    ("TZ", "Mars/Olympus_Mons"),
])
def test_hostile_shell_env(env, var, val):
    gate = 'test -z "${PYTHONHOME-}${PYTHONWARNINGS-}${PYTHONOPTIMIZE-}${PYTEST_ADDOPTS-}${PIP_USER-}" && echo GATE_ENV_CLEAN'
    r = env.run(**{var: val}, SKIP_GATE="0", GATE_CMD=gate)
    assert r.returncode == 0, out(r)
    assert "GATE_ENV_CLEAN" in _gate_out(env)


def test_pythonpath_shadow_package_ignored(env, tmp_path):
    shadow = tmp_path / "shadow" / "avrone"; shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise SystemExit('SHADOWED_AVRONE')\n")
    env.flag("chat_smoke_pass")
    r = env.run(PYTHONPATH=str(shadow.parent))
    assert r.returncode == 0, out(r)
    assert "SHADOWED_AVRONE" not in out(r)
    assert "SHADOWED_AVRONE" not in "".join(f.read_text(errors="replace") for f in (env.drive / "avrone-runs").rglob("*.err"))


# ---------------- exFAT / FAT USB drive quirks ----------------

def test_appledouble_patch_files_ignored(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    pd = e.repo / "scripts/avrone-state/patches"
    for p in sorted(pd.glob("*.patch")):
        (pd / ("._" + p.name)).write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + os.urandom(64))
    _git(e.repo, "add", "-A"); _git(e.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "appledouble")
    r = e.run()
    assert r.returncode == 0, out(r)
    assert "patch apply failed" not in out(r)


def test_macos_drive_metadata_dirs_harmless(env):
    for d in (".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems"):
        (env.drive / d).mkdir(parents=True, exist_ok=True)
    env.state.mkdir(parents=True, exist_ok=True)
    (env.state / "._reintro-stage.txt").write_bytes(b"\x00\x05\x16\x07junk")
    (env.drive / "avrone-runs").mkdir(exist_ok=True)
    (env.drive / "avrone-runs" / "._20261101-010000-1").write_bytes(b"\x00\x05\x16\x07")
    r = env.run(KEEP_RUNS="1")
    assert r.returncode == 0, out(r)
    assert env.stage() == "7"


# ---------------- DST fall-back: prune by age, never by name ----------------

def test_prune_uses_age_not_name_across_dst(env):
    runs = env.drive / "avrone-runs"; runs.mkdir(parents=True)
    now = time.time()
    # 01:59 EDT happened first; after fall-back the clock read 01:05 again (EST) for the newer run
    first = runs / "20261101-015900-111"; first.mkdir(); (first / "MERGE_ME.md").write_text("older")
    second = runs / "20261101-010500-222"; second.mkdir(); (second / "MERGE_ME.md").write_text("newer")
    os.utime(first, (now - 7200, now - 7200)); os.utime(second, (now - 600, now - 600))
    r = env.run(KEEP_RUNS="2")
    assert r.returncode == 0, out(r)
    assert second.exists(), "the newest earlier run was pruned because its name sorted first"
    assert not first.exists()


# ---------------- every numeric setting validated before anything starts ----------------

@pytest.mark.parametrize("var,val", [
    ("LANE_TIMEOUT", "1h"), ("LANE_TIMEOUT", "0"), ("LANE_TIMEOUT", "9999999999"),
    ("SMOKE_TIMEOUT", "0"), ("SMOKE_TIMEOUT", "30s"),
    ("CYCLE_SLEEP", "-1"), ("CYCLE_SLEEP", "0.5"),
    ("MIN_FREE_GB", "1.5"), ("MIN_FREE_GB", "eighty"),
    ("MIN_RUN_FREE_GB", "abc"), ("MIN_RUN_FREE_GB", " 5"),
    ("KEEP_RUNS", "1e3"), ("KEEP_RUNS", "-2"),
])
def test_bad_numeric_setting_fails_fast(env, var, val):
    r = env.run(**{var: val})
    assert r.returncode == 1, out(r)
    assert var in out(r)
    assert "ollama serve" not in env.calls(), "should fail before starting anything"


@pytest.mark.parametrize("model", ["gpt-\u00f6ss:20b", "qwen\u00e9:7b", "\uff47pt:1", "m\u0131stral:7b"])
def test_non_ascii_model_rejected_in_utf8_locale(env, model):
    r = env.run(MODEL=model, LANG="C.UTF-8", LC_ALL="C.UTF-8")
    assert r.returncode == 1 and "MODEL has illegal characters" in out(r), out(r)


# =====================================================================================
# Round 7: foreign port owners, model tags, unfinished git work, odd checkouts, launchd env
# =====================================================================================

def _foreign_server(port, status, body, ctype="text/html"):
    import http.server, threading
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status); self.send_header("Content-Type", ctype); self.end_headers()
            self.wfile.write(body)
        do_POST = do_GET
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("status,body,ctype", [
    (404, b"<html>Not Found</html>", "text/html"),
    (200, b"hello from some dev server", "text/plain"),
    (200, b'{"ok":true}', "application/json"),
    (403, b"AirPlay", "text/plain"),
    (500, b"", "text/plain"),
])
def test_foreign_service_on_ollama_port(env, status, body, ctype):
    srv = _foreign_server(env.port, status, body, ctype)
    try:
        r = env.run(timeout=90)
    finally:
        srv.shutdown(); srv.server_close()
    assert r.returncode == 1, out(r)
    assert "not Ollama" in out(r)
    assert "ollama pull" not in env.calls() and "ollama serve" not in env.calls()


def test_untagged_model_matches_latest_no_repull(env):
    (env.stub / "models").write_text("stub-model:latest\n")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ollama pull" not in env.calls(), "re-pulled a model that was already present as :latest"


def test_untagged_model_does_not_match_other_tag(env):
    (env.stub / "models").write_text("stub-model:7b\n")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ollama pull stub-model" in env.calls()


def test_tagged_model_does_not_match_prefix(env):
    (env.stub / "models").write_text("stub-model:120b-q8\n")
    r = env.run(MODEL="stub-model:120b")
    assert r.returncode == 0, out(r)
    assert "ollama pull stub-model:120b" in env.calls()


# ---------------- a user's unfinished git work is never built on top of ----------------

@pytest.mark.parametrize("marker,word", [
    ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"),
    ("rebase-merge", "rebase"),
])
@pytest.mark.parametrize("which", ["patched", "unpatched"])
def test_git_mid_operation_refused(tmp_path, base_repos, marker, word, which):
    e = Env(tmp_path, base_repos[which])
    head = _git(e.repo, "rev-parse", "HEAD").stdout.strip()
    m = e.repo / ".git" / marker
    if marker == "rebase-merge":
        m.mkdir(); (m / "head-name").write_text("refs/heads/main\n")
    else:
        m.write_text(head + "\n")
    r = e.run()
    assert r.returncode == 1, out(r)
    assert f"middle of a {word}" in out(r)
    assert _git(e.repo, "rev-parse", "HEAD").stdout.strip() == head
    assert m.exists(), "the user's in-progress operation was destroyed"


def test_real_bisect_in_progress_refused(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    head = _git(e.repo, "rev-parse", "HEAD").stdout.strip()
    _git(e.repo, "bisect", "start", "HEAD", "HEAD~3")
    mid = _git(e.repo, "rev-parse", "HEAD").stdout.strip()
    r = e.run()
    assert r.returncode == 1 and "middle of a bisect" in out(r), out(r)
    assert _git(e.repo, "rev-parse", "HEAD").stdout.strip() == mid
    assert head  # sanity


@pytest.mark.parametrize("branch_exists", [False, True])
def test_detached_head_unpatched_gets_a_branch(tmp_path, base_repos, branch_exists):
    e = Env(tmp_path, base_repos["unpatched"])
    _git(e.repo, "checkout", "-q", "--detach")
    if branch_exists:
        _git(e.repo, "branch", "avrone-autopilot")
    r = e.run()
    assert r.returncode == 0, out(r)
    assert "created branch avrone-autopilot" in out(r)
    br = _git(e.repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    assert br.startswith("avrone-autopilot") and (br != "avrone-autopilot") == branch_exists
    # the patch commits live on that branch, not on a dangling HEAD
    assert "coerce_args" in (e.repo / "avrone/trinity.py").read_text()


def test_detached_head_patched_runs(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"])
    _git(e.repo, "checkout", "-q", "--detach")
    r = e.run()
    assert r.returncode == 0, out(r)


def test_shallow_clone_applies_patches(tmp_path, base_repos):
    src = tmp_path / "shallow-src"
    r0 = subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{base_repos['unpatched']}", str(src)],
                        capture_output=True, text=True)
    if r0.returncode:
        pytest.skip(r0.stderr)
    shutil.copytree(base_repos["unpatched"] / "scripts", src / "scripts", dirs_exist_ok=True)
    _git(src, "config", "user.email", "t@t"); _git(src, "config", "user.name", "t")
    e = Env(tmp_path, src)
    r = e.run()
    assert r.returncode == 0, out(r)
    assert "applying carried patches" in out(r)


def test_git_worktree_checkout(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"])
    wt = tmp_path / "wt"
    assert _git(e.repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt)).returncode == 0
    shutil.copytree(e.repo / "scripts", wt / "scripts", dirs_exist_ok=True)
    shutil.copytree(e.repo / ".venv", wt / ".venv", symlinks=True)
    assert (wt / ".git").is_file()
    r = subprocess.run([BASH, str(wt / SCRIPT_REL)], cwd=tmp_path, env=e.env(), capture_output=True, text=True, timeout=180)
    e.kill_server()
    assert r.returncode == 0, out(r)


def test_repo_without_git_dir(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"])
    shutil.rmtree(e.repo / ".git")
    r = e.run()
    assert r.returncode == 1 and "no git repo" in out(r), out(r)


# ---------------- launchd / cron style environments ----------------

def test_launchd_minimal_path_finds_homebrew(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"])
    pfx = tmp_path / "fake-homebrew"; (pfx / "bin").mkdir(parents=True)
    shutil.move(str(e.bin / "ollama"), str(pfx / "bin" / "ollama"))
    env = e.env(PATH=f"{e.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                AUTOPILOT_BREW_PREFIXES=f"{pfx} {Path(sys.executable).resolve().parent.parent}")
    r = subprocess.run([BASH, str(e.repo / SCRIPT_REL)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    e.kill_server()
    assert r.returncode == 0, out(r)
    assert "brew install" not in e.calls(), "reinstalled ollama that Homebrew already had"


@pytest.mark.parametrize("home", ["__unset__", "/nonexistent/home/crow", ""])
def test_home_unset_or_missing(env, home):
    r = env.run(HOME=home)
    assert r.returncode == 0, out(r)
    assert "HOME was unset or missing" in out(r)
    assert (env.drive / "avrone-home").is_dir()


def test_readonly_home(env, tmp_path):
    h = tmp_path / "ro-home"; h.mkdir(); h.chmod(0o555)
    try:
        r = env.run(HOME=str(h))
    finally:
        h.chmod(0o755)
    assert r.returncode == 0, out(r)


@pytest.mark.parametrize("tmpdir", ["/nonexistent/tmp", "RO"])
def test_bad_tmpdir(env, tmp_path, tmpdir):
    if tmpdir == "RO":
        d = tmp_path / "ro-tmp"; d.mkdir(); d.chmod(0o555); tmpdir = str(d)
    gate = 'python -c "import tempfile; tempfile.mkstemp(); print(\'TMP_OK\')"'
    r = env.run(TMPDIR=tmpdir, SKIP_GATE="0", GATE_CMD=gate)
    assert r.returncode == 0, out(r)
    assert "TMP_OK" in _gate_out(env)


@pytest.mark.parametrize("mask", ["0277", "0377", "0777", "0077"])
def test_hostile_umask(env, mask):
    env.flag("chat_smoke_pass")
    r = subprocess.run([BASH, "-c", f'umask {mask}; exec "$0" "$1"', BASH, str(env.repo / SCRIPT_REL)],
                       cwd=env.tmp, env=env.env(), capture_output=True, text=True, timeout=180)
    env.kill_server()
    assert r.returncode == 0, out(r)
    st = env.state / "reintro-stage.txt"
    assert st.exists() and os.access(st, os.R_OK | os.W_OK)


# =====================================================================================
# Round 8: talking to Ollama when Ollama misbehaves (foreign server, context, pulls, hangs)
# =====================================================================================

class _ForeignOllama:
    """An Ollama the autopilot did not start (stands in for the Ollama menu-bar app)."""
    def __init__(self, env):
        self.env = env
        self.p = subprocess.Popen([str(env.bin / "ollama"), "serve"], env=env.env(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", env.port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
    def run(self, **kw):
        r = subprocess.run([BASH, str(self.env.repo / SCRIPT_REL)], env=self.env.env(**kw),
                           capture_output=True, text=True, timeout=180)
        return r
    def close(self):
        self.p.kill(); self.p.wait()


def _envsh(env): return (env.state / "env.sh").read_text()


def test_foreign_ollama_gets_context_baked_in(env):
    f = _ForeignOllama(env)
    try:
        r = f.run(CTX="32768")
        assert f.p.poll() is None, "killed an Ollama it did not start"
    finally:
        f.close()
    assert r.returncode == 0, out(r)
    assert "ollama create avrone-stub-model:ctx32768" in env.calls()
    assert "PARAMETER num_ctx 32768" in (env.stub / "created_modelfile").read_text()
    assert "avrone-stub-model:ctx32768" in _envsh(env)


def test_foreign_ollama_derived_model_reused_not_recreated(env):
    (env.stub / "models").write_text("stub-model\navrone-stub-model:ctx32768\n")
    f = _ForeignOllama(env)
    try:
        r = f.run(CTX="32768")
    finally:
        f.close()
    assert r.returncode == 0, out(r)
    assert "ollama create" not in env.calls()
    assert "avrone-stub-model:ctx32768" in _envsh(env)


def test_foreign_ollama_create_fails_falls_back(env):
    env.flag("create_fail")
    f = _ForeignOllama(env)
    try:
        r = f.run()
    finally:
        f.close()
    assert r.returncode == 0, out(r)
    assert "could not create" in out(r)
    assert "AVRONE_LLM_MODEL=stub-model" in _envsh(env)


def test_foreign_ollama_missing_model_never_downloads_to_internal_disk(env):
    (env.stub / "models").write_text("")
    f = _ForeignOllama(env)
    try:
        r = f.run()
    finally:
        f.close()
    assert r.returncode == 1, out(r)
    assert "Quit the Ollama app" in out(r)
    assert "ollama pull" not in env.calls()


def test_foreign_ollama_missing_model_reuse_opt_in(env):
    (env.stub / "models").write_text("")
    f = _ForeignOllama(env)
    try:
        r = f.run(REUSE_OLLAMA="1")
    finally:
        f.close()
    assert r.returncode == 0, out(r)
    assert "ollama pull stub-model" in env.calls()
    assert "REUSE_OLLAMA=1" in out(r)


def test_own_ollama_needs_no_derived_model(env):
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ollama create" not in env.calls()
    assert "AVRONE_LLM_MODEL=stub-model" in _envsh(env)


def test_serve_race_with_ollama_app_reuses_winner(env):
    env.flag("serve_race")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "another Ollama came up" in out(r)


def test_hung_ollama_list_has_deadline(env):
    env.flag("list_hang")
    t0 = time.time()
    r = env.run(LIST_TIMEOUT="3", timeout=120)
    assert r.returncode == 1, out(r)
    assert "not answering 'ollama list'" in out(r)
    assert time.time() - t0 < 60


def test_pull_success_but_model_missing(env):
    (env.stub / "models").write_text("")
    env.flag("pull_noop")
    r = env.run(timeout=150)
    assert r.returncode == 1, out(r)
    assert "pull said success but" in out(r)
    assert env.calls().count("ollama pull") == 3


@pytest.mark.parametrize("msg,expect,pulls", [
    ("Error: pull model manifest: 412: The model you are attempting to pull requires a newer version of Ollama.",
     "brew upgrade ollama", 1),
    ("Error: pull model manifest: file does not exist", "not found in the Ollama library", 1),
    ("Error: write /Volumes/AI/ollama-models/blobs/sha256-partial: no space left on device", "ran out of disk space", 1),
    ("Error: pull model manifest: Get \"https://registry.ollama.ai/v2/library/x/manifests/y\": dial tcp: lookup registry.ollama.ai: no such host",
     "network looks down", 3),
    ("Error: something odd happened", "after 3 attempts", 3),
])
def test_pull_failures_classified(env, msg, expect, pulls):
    (env.stub / "models").write_text("")
    env.flag("pull_msg", msg)
    r = env.run(timeout=150)
    assert r.returncode == 1, out(r)
    assert expect in out(r)
    assert env.calls().count("ollama pull") == pulls


# =====================================================================================
# Round 9: listings that lie, offline restarts, settings added in round 8, odd model names
# =====================================================================================

@pytest.mark.parametrize("var", ["LIST_TIMEOUT", "WARM_TIMEOUT"])
@pytest.mark.parametrize("val", ["abc", "-5", "1.5", "0", "9999999999"])
def test_new_timeouts_validated(env, var, val):
    r = env.run(**{var: val}, timeout=60)
    assert r.returncode == 1, out(r)
    assert var in out(r)
    assert "ollama pull" not in env.calls()


def test_failed_listing_never_triggers_redownload(env):
    env.flag("list_fail")
    r = env.run(timeout=90)
    assert r.returncode == 1, out(r)
    assert "keeps failing" in out(r)
    assert "ollama pull" not in env.calls()
    assert env.calls().count("ollama list") == 3


def test_flaky_listing_recovers(env):
    """first `ollama list` errors (server still loading), the retry succeeds"""
    env.flag("list_fail")
    fl = env.stub / "list_fail"
    killer = subprocess.Popen(["bash", "-c", f"while ! grep -q 'ollama list' {env.stub}/calls.log 2>/dev/null; do sleep 0.05; done; rm -f {fl}"])
    try:
        r = env.run(timeout=90)
    finally:
        killer.kill()
    assert r.returncode == 0, out(r)
    assert "ollama pull" not in env.calls()


def test_headerless_listing_does_not_hide_first_model(env):
    env.flag("list_noheader")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ollama pull" not in env.calls()


def test_model_named_like_header_column_still_found(env):
    (env.stub / "models").write_text("name-model:7b\n")
    r = env.run(MODEL="name-model:7b")
    assert r.returncode == 0, out(r)
    assert "ollama pull" not in env.calls()


def _failing_pip(env):
    p = env.bin / "pip"
    p.write_text("#!/bin/sh\necho 'ERROR: Could not find a version that satisfies the requirement setuptools (offline)' >&2\nexit 1\n")
    p.chmod(0o755)


def test_offline_rerun_with_deps_installed_continues(env):
    _failing_pip(env)
    r = env.run(AUTOPILOT_NO_INSTALL="0")
    assert r.returncode == 0, out(r)
    assert "offline?" in out(r)


def test_offline_first_run_without_deps_fails_clearly(env):
    _failing_pip(env)
    vpy = env.repo / ".venv" / "bin" / "python"
    vpy.unlink()
    vpy.write_text(f'#!/bin/sh\ncase "$*" in *"import avrone"*) exit 1;; esac\nexec "{sys.executable}" "$@"\n')
    vpy.chmod(0o755)
    r = env.run(AUTOPILOT_NO_INSTALL="0")
    assert r.returncode == 1, out(r)
    assert "connect to the internet" in out(r)


def test_dangling_venv_python_after_brew_upgrade_is_rebuilt(env):
    vpy = env.repo / ".venv" / "bin" / "python"
    vpy.unlink()
    vpy.symlink_to("/opt/homebrew/Cellar/python@3.11/3.11.0/bin/python3.11-gone")
    r = env.run()
    assert "rebuilding old/broken .venv" in out(r)
    assert (env.repo / ".venv" / "bin" / "activate").stat().st_size > 0


def test_hf_style_model_gets_valid_derived_name(env):
    m = "hf.co/Bartowski/Qwen2.5-Coder:Q4_K_M"
    (env.stub / "models").write_text(m + "\n")
    srv = subprocess.Popen([str(env.bin / "ollama"), "serve"], env=env.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", env.port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        r = subprocess.run([BASH, str(env.repo / SCRIPT_REL)], env=env.env(MODEL=m, CTX="16384"),
                           capture_output=True, text=True, timeout=180)
    finally:
        srv.kill(); srv.wait()
    assert r.returncode == 0, out(r)
    d = "avrone-hf.co-bartowski-qwen2.5-coder-q4_k_m:ctx16384"
    assert f"ollama create {d}" in env.calls()
    assert d == d.lower() and d.count(":") == 1 and "/" not in d
    assert f"FROM {m}" in (env.stub / "created_modelfile").read_text()


def test_ctx_change_makes_new_derived_model(env):
    (env.stub / "models").write_text("stub-model\navrone-stub-model:ctx8192\n")
    srv = subprocess.Popen([str(env.bin / "ollama"), "serve"], env=env.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", env.port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        r = subprocess.run([BASH, str(env.repo / SCRIPT_REL)], env=env.env(CTX="32768"),
                           capture_output=True, text=True, timeout=180)
    finally:
        srv.kill(); srv.wait()
    assert r.returncode == 0, out(r)
    assert "ollama create avrone-stub-model:ctx32768" in env.calls()
    assert "avrone-stub-model:ctx32768" in (env.state / "env.sh").read_text()


# =====================================================================================
# Round 10: honest lane numbers, a report that can't freeze the loop, drive tidying, log growth
# =====================================================================================

def _lane_line(r, lane="tools"):
    return [l for l in out(r).splitlines() if f"lane {lane} rc=" in l][-1]


def test_lane_counts_ignore_log_noise(env):
    env.flag("chat_err_text")
    r = env.run(LANES="tools")
    assert r.returncode == 0, out(r)
    assert "proposals=0 accepted=0" in _lane_line(r)


def test_lane_counts_come_from_proposal_records(env):
    ev = env.drive / "avrone-data" / "lane-tools" / "evolve"; ev.mkdir(parents=True)
    old = ev / "p-old.json"; old.write_text('{"pid": "p-old", "passed": true}')
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    env.flag("chat_fake_accept")
    r = env.run(LANES="tools")
    assert r.returncode == 0, out(r)
    assert "proposals=2 accepted=1" in _lane_line(r), _lane_line(r)


def test_hung_proposal_listing_does_not_freeze_report(env):
    env.flag("evolve_hang")
    t0 = time.time()
    r = env.run(LANES="tools", REPORT_TIMEOUT="3", CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert out(r).count("=== cycle") == 2
    assert "listing timed out" in (list((env.drive / "avrone-runs").glob("*/MERGE_ME.md"))[0]).read_text()
    assert time.time() - t0 < 150


def test_merge_me_blocked_by_folder_keeps_looping(env):
    (env.repo / "MERGE_ME.md").mkdir()
    r = env.run(LANES="tools", CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert out(r).count("=== cycle") == 2
    assert "could not update" in out(r)
    assert list((env.drive / "avrone-runs").glob("*/MERGE_ME.md"))
    assert (env.repo / "MERGE_ME.md").is_dir()


def test_merge_me_readonly_file_keeps_looping(env):
    m = env.repo / "MERGE_ME.md"; m.write_text("old"); 
    os.chmod(env.repo, 0o555)
    try:
        r = env.run(LANES="tools")
    finally:
        os.chmod(env.repo, 0o755)
    assert r.returncode == 0, out(r)
    assert "could not update" in out(r)
    assert not (env.repo / "MERGE_ME.md.tmp").exists()


def test_merge_me_written_atomically_in_repo(env):
    r = env.run(LANES="tools")
    assert r.returncode == 0, out(r)
    assert (env.repo / "MERGE_ME.md").read_text().startswith("# AVRONE")
    assert not (env.repo / "MERGE_ME.md.tmp").exists()
    assert "MERGE_ME.md" not in _git(env.repo, "status", "--porcelain").stdout


def test_run_folder_deleted_mid_run_is_recreated(env):
    env.flag("chat_rm_runs")
    r = env.run(LANES="tools")
    assert r.returncode == 0, out(r)
    logs = list((env.drive / "avrone-runs").glob("*/autopilot.log"))
    assert logs and "done. results in" in logs[0].read_text()


def test_big_ollama_log_rotated(env):
    f = env.drive / "ollama.log"
    with open(f, "wb") as fh: fh.truncate(3 * 1048576)
    r = env.run(OLLAMA_LOG_MAX_MB="2")
    assert r.returncode == 0, out(r)
    assert "rotated ollama.log (3MB)" in out(r)
    assert (env.drive / "ollama.log.1").stat().st_size == 3 * 1048576
    assert f.stat().st_size < 1048576


def test_small_ollama_log_left_alone(env):
    f = env.drive / "ollama.log"; f.write_text("keep me\n")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "rotated" not in out(r)
    assert f.read_text().startswith("keep me")


@pytest.mark.parametrize("var", ["OLLAMA_LOG_MAX_MB", "REPORT_TIMEOUT"])
@pytest.mark.parametrize("val", ["0", "abc", "1+1", "a[$(touch /tmp/pwned-r10)]"])
def test_round10_settings_validated(env, var, val):
    r = env.run(**{var: val}, timeout=60)
    assert r.returncode == 1, out(r)
    assert var in out(r)
    assert not os.path.exists("/tmp/pwned-r10")


# =====================================================================================
# Round 11: crash loops, and the repo changing under a running loop
# =====================================================================================

def test_crash_loop_stops_with_the_real_error(env):
    env.flag("chat_modnotfound")
    r = env.run(LANES="tools", CYCLES="0", ONCE="0", timeout=150)
    assert r.returncode == 1, out(r)
    assert "crashed on every run for 3 cycles" in out(r)
    assert "ModuleNotFoundError: No module named 'newdep'" in out(r)
    assert out(r).count("=== cycle") == 3


def test_crash_limit_setting(env):
    env.flag("chat_modnotfound")
    r = env.run(LANES="tools", CYCLES="0", ONCE="0", CRASH_LIMIT="1", timeout=90)
    assert r.returncode == 1 and "for 1 cycles" in out(r) and out(r).count("=== cycle") == 1


def test_crash_loop_breaker_counts_smoke_alone(env):
    # no lanes selected: the smoke run alone crashing every cycle still trips the breaker
    env.flag("chat_modnotfound")
    r = env.run(LANES="", CYCLES="0", ONCE="0", CRASH_LIMIT="2", timeout=90)
    assert r.returncode == 1 and "crashed on every run for 2 cycles" in out(r)


def test_timeouts_are_not_crashes(env):
    env.flag("chat_hang")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0", CRASH_LIMIT="1", SMOKE_TIMEOUT="2", LANE_TIMEOUT="2", timeout=120)
    assert r.returncode == 0, out(r)
    assert "crashed on every run" not in out(r)


def test_healthy_runs_never_trip_the_breaker(env):
    env.flag("chat_smoke_pass")
    r = env.run(LANES="tools", CYCLES="3", ONCE="0", CRASH_LIMIT="1")
    assert r.returncode == 0, out(r)
    assert "every agent run crashed" not in out(r)


def test_completed_reintro_with_no_lanes_is_not_a_crash(env):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text("9")
    env.flag("chat_modnotfound")
    r = env.run(LANES="", CYCLES="3", ONCE="0", CRASH_LIMIT="1")
    assert r.returncode == 0, out(r)
    assert "reintro complete" in out(r)


@pytest.mark.parametrize("val", ["0", "x", "2+2", "a[$(touch /tmp/pwned-r11)]"])
def test_crash_limit_validated(env, val):
    r = env.run(CRASH_LIMIT=val, timeout=60)
    assert r.returncode == 1 and "CRASH_LIMIT" in out(r)
    assert not os.path.exists("/tmp/pwned-r11")


def test_repo_head_moving_mid_run_is_logged(env):
    env.flag("chat_git_commit")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "repo HEAD moved" in out(r)


def test_patches_vanishing_mid_run_stops(base_repos, tmp_path):
    e = Env(tmp_path, base_repos["unpatched"])
    try:
        base = _git(e.repo, "rev-parse", "HEAD").stdout.strip()
        e.flag("chat_git_reset", base)
        r = e.run(CYCLES="2", ONCE="0")
        assert r.returncode == 1, out(r)
        assert "carried patches are gone" in out(r)
        assert out(r).count("=== cycle") == 2
    finally:
        e.kill_server()


def test_partial_crash_does_not_trip_the_breaker(env):
    # smoke works, every lane crashes: not EVERY run crashed, so the loop keeps going
    env.flag("chat_crash_lanes")
    r = env.run(LANES="tools robust", CYCLES="3", ONCE="0", CRASH_LIMIT="1")
    assert r.returncode == 0, out(r)
    assert out(r).count("lane tools rc=3") == 3 and "every agent run crashed" not in out(r)


# =====================================================================================
# Round 12: the smoke judge counts only real event lines; stalled reintro is loud
# =====================================================================================

def _ev(kind, detail, agent="Assistant-1"): return f"[ab12cd34ef] {kind:<14} {agent:<18} {detail}"


class TestJudgeEventLines:
    def test_real_event_format_counts(self, tmp_path):
        log = "\n".join(_ev("tool.call", f"{t} {{}}") for t in ("a", "b"))
        assert "2/2 exercised" in _judge(tmp_path, log, "a,b")[0]

    def test_call_text_inside_a_tool_result_is_not_a_call(self, tmp_path):
        log = _ev("tool.result", "12:00 tool.call agent-1 a {} tool.call agent-1 b {}")
        out_, st, _ = _judge(tmp_path, log, "a,b")
        assert "0/2" in out_ and "HOLD" in out_ and st == "7"

    def test_call_text_in_agent_narration_is_not_a_call(self, tmp_path):
        log = _ev("agent.done", "I did tool.call x a and tool.call x b")
        assert "0/2" in _judge(tmp_path, log, "a,b")[0]

    def test_narrated_unknown_tool_is_not_an_error(self, tmp_path):
        log = _ev("tool.call", "a {}") + "\n" + "\n".join(_ev("agent.done", "no unknown tool errors seen") for _ in range(5))
        out_ = _judge(tmp_path, log, "a")[0]
        assert "malformed/unknown=0" in out_ and "ADVANCE" in out_

    def test_unknown_tool_in_tool_result_counts(self, tmp_path):
        log = _ev("tool.call", "a {}") + "\n" + "\n".join(_ev("tool.result", "ERROR: unknown tool 'zz'. Known: a") for _ in range(4))
        out_ = _judge(tmp_path, log, "a")[0]
        assert "malformed/unknown=4" in out_ and "HOLD" in out_

    def test_malformed_mentioned_in_output_is_not_counted(self, tmp_path):
        log = _ev("tool.call", "a {}") + "\n" + "\n".join(_ev("tool.result", "grep action.malformed log") for _ in range(5))
        assert "malformed/unknown=0" in _judge(tmp_path, log, "a")[0]

    def test_malformed_event_counts(self, tmp_path):
        log = _ev("tool.call", "a {}") + "\n" + "\n".join(_ev("action.malformed", "{}") for _ in range(4))
        assert "malformed/unknown=4" in _judge(tmp_path, log, "a")[0]

    def test_backend_5xx_hint(self, tmp_path):
        log = "\n".join(_ev("error", "Model backend error 500: error parsing tool call") for _ in range(3))
        out_ = _judge(tmp_path, log, "a,b")[0]
        assert "HOLD" in out_ and "5xx 3x" in out_ and "not the tools" in out_

    def test_no_hint_when_advancing(self, tmp_path):
        log = _ev("tool.call", "a {}") + "\n" + _ev("error", "Model backend error 502: x")
        out_ = _judge(tmp_path, log, "a")[0]
        assert "ADVANCE" in out_ and "HINT" not in out_

    def test_4xx_is_not_a_backend_hint(self, tmp_path):
        assert "HINT" not in _judge(tmp_path, _ev("error", "Model backend error 404: model not found"), "a")[0]


def test_spoofed_calls_in_tool_output_never_advance_the_stage(env):
    env.flag("chat_spoof")
    r = env.run(LANES="tools")
    assert r.returncode == 0, out(r)
    assert "smoke: 0/" in out(r) and "HOLD" in out(r) and "ADVANCE" not in out(r)
    st = re.search(r"reintro stage (\d+)/", out(r)).group(1)
    assert (env.state / "reintro-stage.txt").read_text().strip() == st
    assert re.search(r"lane tools rc=\d+ calls=0\b", out(r)), [l for l in out(r).splitlines() if "lane tools" in l]


def test_stalled_reintro_warns_every_stall_cycles(env):
    env.flag("chat_noop")
    r = env.run(CYCLES="4", ONCE="0", STALL_CYCLES="2")
    assert r.returncode == 0, out(r)
    assert len(re.findall(r"reintro stuck at stage \d+/", out(r))) == 2
    assert "for 2 cycles" in out(r) and "for 4 cycles" in out(r)
    assert "STUCK for 4 cycles" in (env.repo / "MERGE_ME.md").read_text()


def test_progress_resets_the_stall_counter(env):
    env.flag("chat_smoke_pass")
    r = env.run(CYCLES="3", ONCE="0", STALL_CYCLES="1")
    assert r.returncode == 0, out(r)
    assert "reintro stuck" not in out(r) and "STUCK" not in (env.repo / "MERGE_ME.md").read_text()


@pytest.mark.parametrize("val", ["0", "-1", "abc", "1e3"])
def test_stall_cycles_validated(env, val):
    r = env.run(STALL_CYCLES=val, timeout=60)
    assert r.returncode == 1 and "STALL_CYCLES" in out(r)


# =====================================================================================
# Round 13: the 5xx hint must also read the gateway's error spelling
# =====================================================================================

class TestJudgeGatewayErrors:
    def test_gateway_spelling_triggers_the_hint(self, tmp_path):
        msg = ("All upstreams failed (ollama:fail): ollama 500: {\"error\":\"error parsing tool call\"} | "
               "ollama 500: {\"error\":\"error parsing tool call\"}")
        out_ = _judge(tmp_path, _ev("error", msg), "a")[0]
        assert "HOLD" in out_ and "5xx 2x" in out_

    def test_gateway_4xx_no_hint(self, tmp_path):
        assert "HINT" not in _judge(tmp_path, _ev("error", "All upstreams failed (ollama:fail): ollama 404: no model"), "a")[0]

    def test_port_number_is_not_a_status(self, tmp_path):
        # "127.0.0.1:11500" or "ollama:11500" must not read as a 5xx answer
        log = _ev("error", "ollama unreachable: All connection attempts failed http://127.0.0.1:11500")
        assert "HINT" not in _judge(tmp_path, log, "a")[0]


# =====================================================================================
# Round 14: truncated (finish_reason=length) replies get their own hint
# =====================================================================================

class TestJudgeTruncation:
    def test_truncation_hint_on_hold(self, tmp_path):
        msg = ("All upstreams failed (ollama:fail): ollama 200: empty reply, output truncated "
               "(finish_reason=length); the context window or max tokens is too small for this model")
        out_ = _judge(tmp_path, _ev("error", msg), "a")[0]
        assert "HOLD" in out_ and "cut off" in out_ and "CTX" in out_
        assert "5xx" not in out_  # a 200 is not a server failure

    def test_no_truncation_hint_when_advancing(self, tmp_path):
        log = _ev("tool.call", "a") + _ev("error", "x finish_reason=length")
        assert "cut off" not in _judge(tmp_path, log, "a")[0]
