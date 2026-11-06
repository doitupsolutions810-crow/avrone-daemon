"""Proposal E: headless smoke test for the visual layer. Loopback only."""
from __future__ import annotations

import importlib.util
import os
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARM = os.path.join(ROOT, "harmonic", "harmonic_dash.py")


def _load_dash():
    spec = importlib.util.spec_from_file_location("harmonic_dash", HARM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve(directory, port):
    os.makedirs(directory, exist_ok=True)
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=directory, **k)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    httpd._smoke_thread = t
    return httpd


def _close_server(httpd):
    """Stop the serve loop and close the listening socket deterministically."""
    httpd.shutdown()
    httpd.server_close()
    thread = getattr(httpd, "_smoke_thread", None)
    if thread is not None:
        thread.join(timeout=2)
        if thread.is_alive():
            raise RuntimeError("harmonic smoke HTTP server did not stop")


def test_gauge_smoke_injected_index():
    dash = _load_dash()
    injected = dash.inject_into_index(
        "<html><head><title>AVRONE</title></head><body><h1>HUD</h1></body></html>"
    )
    port = _free_port()
    tmp = os.path.join(ROOT, ".smoke-tmp")
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "index.html"), "w") as f:
        f.write(injected)
    httpd = _serve(tmp, port)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/index.html", timeout=5.0)
        assert r.status_code == 200
        body = r.text
        assert body.count('id="harmonic-thermo-gauge"') == 1
        assert "https://d3js.org/d3.v7.min.js" in body
        assert "setDebt" in body and "d3.arc" in body and "/api/codex/events" in body
        assert dash.inject_into_index(injected).count('id="harmonic-thermo-gauge"') == 1
    finally:
        _close_server(httpd)


def test_gauge_smoke_missing_d3_graceful():
    injected = "<html><body></body></html>".replace(
        "</body>",
        '<div id="harmonic-thermo-gauge"></div>\n<script>/* d3 unavailable */</script>\n</body>',
    )
    port = _free_port()
    tmp = os.path.join(ROOT, ".smoke-tmp")
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "index.html"), "w") as f:
        f.write(injected)
    httpd = _serve(tmp, port)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/index.html", timeout=5.0)
        assert r.status_code == 200 and 'id="harmonic-thermo-gauge"' in r.text
    finally:
        _close_server(httpd)
