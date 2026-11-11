"""Round 154: the bridge journal and the server log are redacted too.

On v0.146 only CodeX events passed through ctxspawn.redact.
- Bridge.post stored the raw text in the journal, which is relayed to every peer and to the
  fusion evidence bus. A finding that quoted a config ("token=...") shipped verbatim, and so did
  a peer's unredacted post that this node merged and then re-exported.
- avrone.* log records (MCP stderr, peer error bodies, exceptions quoting URLs with credentials)
  were escaped in round 150 but never redacted.
"""
from __future__ import annotations

import io
import logging

import avrone  # noqa: F401  installs logsafe
from avrone.bridge import Bridge

PW = "hunter2pass"


def test_post_text_and_journal_redacted(tmp_path):
    b = Bridge("r1", tmp_path)
    p = b.post("a", "finding", f"config has password={PW} and curl -u admin:{PW}")
    assert PW not in p["text"] and "admin:" in p["text"]
    assert all(PW not in f.read_text() for f in tmp_path.rglob("*") if f.is_file())
    assert PW not in str(b.export())


def test_merged_peer_post_redacted_before_reexport(tmp_path):
    b = Bridge("r1", tmp_path)
    st = b.merge_remote([{"id": "abcdef123456", "kind": "finding", "text": f"peer: token={PW}", "refs": []}], "peer")
    assert st["merged"] == 1
    assert PW not in b.get("abcdef123456")["text"]
    assert PW not in str(Bridge("r1", tmp_path).export())


def test_replay_keeps_text_without_reprocessing(tmp_path):
    b = Bridge("r1", tmp_path)
    b.post("a", "finding", "plain finding: router at 10.0.0.1 answers")
    assert Bridge("r1", tmp_path).posts()[0]["text"] == "plain finding: router at 10.0.0.1 answers"


def _capture():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("avrone.round154")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, h, buf


def test_log_args_message_and_exceptions_redacted():
    lg, h, buf = _capture()
    try:
        lg.warning("mcp %s stderr: %s", "srv", "using token ghp_" + "a" * 36)
        lg.warning(f"peer said curl -u a:{PW}")
        lg.warning("x %(v)s", {"v": f"password={PW}"})
        try:
            raise RuntimeError(f"https://u:{PW}@h/")
        except RuntimeError as e:
            lg.warning("fail: %s", e)
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert PW not in out and "ghp_" + "a" * 36 not in out
    assert "srv" in out and out.count("\n") == 4


def test_other_loggers_untouched():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("thirdparty.round154")
    lg.addHandler(h)
    lg.propagate = False
    try:
        lg.warning("%s", f"password={PW}")
    finally:
        lg.removeHandler(h)
    assert PW in buf.getvalue()
