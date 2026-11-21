# CI gate report (auto-generated)

- run: https://github.com/doitupsolutions810-crow/avrone-daemon/actions/runs/44963746450
- pytest exit: 1

```text
FAILED tests/test_master_prompt.py::test_committed_prompt_is_current - AssertionError: run: python tools/make_master_prompt.py (clean build differs at char 10005: committed 'calculator.py\n│   ├── datetime_tool.py\n│   ├── json_tool.py\n' vs clean '__pycache__\n│   ├── calculator.py\n│   ├── datetime_tool.py\n│')
...(tail)
........................................................................ [ 87%]
........................................................................ [ 87%]
........................................................................ [ 88%]
........................................................................ [ 88%]
........................................................................ [ 89%]
........................................................................ [ 90%]
........................................................................ [ 90%]
........................................................................ [ 91%]
........................................................................ [ 91%]
........................................................................ [ 92%]
........................................................................ [ 93%]
........................................................................ [ 93%]
........................................................................ [ 94%]
........................................................................ [ 94%]
........................................................................ [ 95%]
........................................................................ [ 96%]
........................................................................ [ 96%]
........................................................................ [ 97%]
........................................................................ [ 97%]
........................................................................ [ 98%]
........................................................................ [ 99%]
........................................................................ [ 99%]
...........................................                              [100%]
=================================== FAILURES ===================================
_______________________ test_committed_prompt_is_current _______________________

    def test_committed_prompt_is_current():
        text = (ROOT / "MASTER_PROMPT.md").read_text(encoding="utf-8")
        if text == MOD["build"]():  # fast path: pure, in-process
            return
        # a long pytest run can leave import/env side effects behind (fake modules stuffed into
        # sys.modules, patched registries the generator consults); judge the committed file in a
        # clean interpreter before calling it stale.
        clean = _clean_build()
        if text == clean:
            return
        first = next((i for i, (a, b) in enumerate(zip(text, clean)) if a != b), min(len(text), len(clean)))
>       raise AssertionError("run: python tools/make_master_prompt.py (clean build differs at char "
                             + str(first) + ": committed " + repr(text[first:first + 60])
                             + " vs clean " + repr(clean[first:first + 60]) + ")")
E       AssertionError: run: python tools/make_master_prompt.py (clean build differs at char 10005: committed 'calculator.py\n│   ├── datetime_tool.py\n│   ├── json_tool.py\n' vs clean '__pycache__\n│   ├── calculator.py\n│   ├── datetime_tool.py\n│')

tests/test_master_prompt.py:37: AssertionError
=============================== warnings summary ===============================
../../../../../opt/hostedtoolcache/Python/3.12.16/x64/lib/python3.12/site-packages/fastapi/testclient.py:1
  /opt/hostedtoolcache/Python/3.12.16/x64/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

tests/test_round43_crash_peer_edges.py: 4 warnings
tests/test_round44_fs_pack_process_edges.py: 35 warnings
tests/test_round45_queue_codex_edges.py: 16 warnings
tests/test_round46_ws_bridge_plugin_edges.py: 4 warnings
tests/test_round48_mem_osint_presence_kit_edges.py: 4 warnings
  /opt/hostedtoolcache/Python/3.12.16/x64/lib/python3.12/multiprocessing/popen_fork.py:66: DeprecationWarning: This process (pid=2258) is multi-threaded, use of fork() may lead to deadlocks in the child.
    self.pid = os.fork()

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_master_prompt.py::test_committed_prompt_is_current - AssertionError: run: python tools/make_master_prompt.py (clean build differs at char 10005: committed 'calculator.py\n│   ├── datetime_tool.py\n│   ├── json_tool.py\n' vs clean '__pycache__\n│   ├── calculator.py\n│   ├── datetime_tool.py\n│')
1 failed, 11820 passed, 318 skipped, 64 warnings in 348.29s (0:05:48)
```
