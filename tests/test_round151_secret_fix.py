"""Round 151: secret_scan fix=true keeps run logs valid, and xAI keys are found.

v0.143:
- fix_runs redacted each flagged raw JSON line as text. If a private key had no END marker
  (for example, tool output cut at a cap), it was masked to the end of the line. That removed
  the closing quotes and braces, so every run log reader skipped the event.
- The file was decoded with errors="replace", so bytes that were not UTF-8 in unflagged lines
  became U+FFFD.
- The rewrite was swapped in without an fsync.
- `xai-` keys (xAI / Grok, which AVRONE uses) matched no pattern. They were neither redacted on
  emit nor flagged.
"""
from __future__ import annotations

import json

from avrone.ctxspawn import redact
from avrone.tools_secrets import _kinds, fix_runs

XAI = "xai-" + "Ab3" * 27


def test_xai_key_redacted_and_flagged():
    assert XAI not in redact(f"key {XAI} end")
    assert _kinds(f"key {XAI}")


def test_fix_keeps_json_valid_for_unterminated_private_key(tmp_path):
    ev = {"kind": "tool.result", "agent": "a", "data": {"output": "x -----BEGIN RSA PRIVATE KEY-----\nMIIE", "n": 1}}
    (tmp_path / "r1.jsonl").write_text(json.dumps(ev) + "\n" + json.dumps({"kind": "after"}) + "\n")
    assert fix_runs(tmp_path, {"r1.jsonl"}) == 1
    rows = [json.loads(x) for x in (tmp_path / "r1.jsonl").read_text().splitlines()]
    assert rows[0]["data"]["n"] == 1 and "MIIE" not in rows[0]["data"]["output"]
    assert rows[1] == {"kind": "after"}


def test_fix_keeps_non_utf8_bytes_of_other_lines(tmp_path):
    good = json.dumps({"kind": "k", "data": {"o": XAI}}).encode()
    junk = b'{"kind":"torn","x":"\xff\xfe\xc3"}'
    (tmp_path / "r2.jsonl").write_bytes(good + b"\n" + junk + b"\n")
    assert fix_runs(tmp_path, {"r2.jsonl"}) == 1
    out = (tmp_path / "r2.jsonl").read_bytes()
    assert junk in out and XAI.encode() not in out


def test_fix_keeps_crlf_and_plain_text_lines(tmp_path):
    (tmp_path / "r3.jsonl").write_bytes(b"not json token=" + XAI.encode() + b"\r\n" + b'{"a":1}\r\n')
    assert fix_runs(tmp_path, {"r3.jsonl"}) == 1
    out = (tmp_path / "r3.jsonl").read_bytes()
    assert out.endswith(b'{"a":1}\r\n') and XAI.encode() not in out and out.count(b"\r\n") == 2
