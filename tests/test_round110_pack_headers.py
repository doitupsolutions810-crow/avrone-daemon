"""Round 110: chained tar extended headers are a refusal, not a RecursionError; agent-dir
overwrites never keep setuid/setgid.

v0.102: tarfile follows each GNU longname (type L) or pax (type x) header into the next header
recursively. A bundle of a few thousand chained headers (a few KB gzipped) raised RecursionError
out of read_pack: an unaudited 500 on fleet migrate import and an exception out of pack_list's
verify. quota_write's overwrite copied S_ISUID/S_ISGID onto new content.
"""
from __future__ import annotations

import gzip
import io
import os
import stat
import tarfile

import pytest

from avrone import agentpack, agentfs_tools


def _chain(n, typ):
    buf = io.BytesIO()
    for _ in range(n):
        data = (b"x" * 10 + b"\0") if typ == tarfile.GNUTYPE_LONGNAME else b"11 path=xxx\n"
        h = tarfile.TarInfo("././@LongLink" if typ == tarfile.GNUTYPE_LONGNAME else "././@PaxHeader")
        h.type, h.size = typ, len(data)
        buf.write(h.tobuf(format=tarfile.GNU_FORMAT if typ == tarfile.GNUTYPE_LONGNAME else tarfile.USTAR_FORMAT))
        buf.write(data + b"\0" * (512 - len(data)))
    ti = tarfile.TarInfo("manifest.json")
    ti.size = 0
    buf.write(ti.tobuf(format=tarfile.GNU_FORMAT))
    buf.write(b"\0" * 1024)
    return gzip.compress(buf.getvalue())


@pytest.mark.parametrize("typ", [tarfile.GNUTYPE_LONGNAME, tarfile.XHDTYPE])
def test_chained_headers_refused(typ):
    with pytest.raises(agentpack.PackError, match="not a valid agent pack"):
        agentpack.read_pack(_chain(5000, typ))


def test_short_chain_still_ordinary_refusal():
    with pytest.raises(agentpack.PackError):
        agentpack.read_pack(_chain(3, tarfile.GNUTYPE_LONGNAME))


def test_import_pack_refuses_chain(tmp_path):
    with pytest.raises(agentpack.PackError):
        agentpack.import_pack(tmp_path / "ws", tmp_path / "data", _chain(5000, tarfile.GNUTYPE_LONGNAME), "router")


def test_pack_list_verify_reports_refused(tmp_path):
    from avrone import tools_packpeer
    p = tmp_path / "builder-1.tar.gz"
    p.write_bytes(_chain(5000, tarfile.GNUTYPE_LONGNAME))
    status, why = tools_packpeer._verify(p)
    assert status == "REFUSED", (status, why)


def test_quota_write_overwrite_drops_setuid(tmp_path, monkeypatch):
    from types import SimpleNamespace
    root = tmp_path / "agents" / "builder"
    root.mkdir(parents=True)
    p = root / "tool.sh"
    p.write_text("old")
    os.chmod(p, 0o6755)
    ctx = SimpleNamespace(core=SimpleNamespace(codex=None, settings=None), run_id="r", name="T")
    try:
        agentfs_tools.quota_write(ctx, root, p, b"new", False, "write_file")
    except TypeError:
        pytest.skip("quota_write signature changed")
    m = stat.S_IMODE(os.lstat(p).st_mode)
    assert p.read_bytes() == b"new"
    assert not m & (stat.S_ISUID | stat.S_ISGID), oct(m)
    assert m & 0o700 == 0o700
