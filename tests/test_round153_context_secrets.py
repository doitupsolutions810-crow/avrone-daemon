"""Round 153: secrets known by the words around them (secretshapes.CONTEXT_RULES).

On v0.145, ctxspawn.redact (applied to every CodeX event) let these through unmasked. They show
up whenever an agent echoes a shell command or an HTTP exchange:
`curl -u user:pass`, `--password X`, `mysql -pX`, `sshpass -p X`, Cookie and Set-Cookie headers,
`redis://:pw@host` (a URL with an empty user), and `<password>x</password>`.
Also `password => 'x'` masked only the ">" and left the value.
"""
from __future__ import annotations

import json
import time

import pytest

from avrone.ctxspawn import redact
from avrone.tools_secrets import _kinds

PW = "hunter2pass"
LEAKED = [
    f"curl -u admin:{PW} https://x", f"curl --user admin:{PW}", f"mysql -u root -p{PW} db",
    f"mysqldump --single-transaction -p{PW} db", f"--password {PW}", f"tool --api-key {PW}",
    f"sshpass -p {PW} ssh host", f"Cookie: session={PW}", f"Set-Cookie: sid={PW}; Path=/",
    f"redis://:{PW}@h:6379", f"password => '{PW}'", f"<password>{PW}</password>", f"<api_key>{PW}</api_key>",
]


@pytest.mark.parametrize("text", LEAKED)
def test_masked_and_flagged(text):
    assert PW not in redact(text)
    assert _kinds(text)


def test_user_name_kept():
    assert redact(f"curl -u admin:{PW} x") == "curl -u admin:[REDACTED] x"


@pytest.mark.parametrize("text", ["ls -a --all", "git log -u", "tar -a x:y", "--password -x",
                                  "rsync a b --password-file f", "mysql -h db", "x <b>bold</b>"])
def test_ordinary_text_untouched(text):
    assert redact(text) == text


def test_json_line_stays_valid():
    line = json.dumps({"o": f"run: curl -u a:{PW} x\n<token>{PW}</token>\n--token {PW}"})
    out = redact(line)
    json.loads(out)
    assert PW not in out


def test_linear_on_hostile_text():
    t = time.perf_counter()
    for big in ["-u " * 100_000, "--" * 100_000, "mysql " * 30_000 + "x", "Cookie:" * 50_000,
                "</" * 100_000, "<password>" + "a" * 100_000, "-u a:" * 50_000]:
        redact(big)
    assert time.perf_counter() - t < 3.0


def test_walrus_and_arrow_separators():
    assert redact("token := abc123") == "token := [REDACTED]"
    assert redact("tokenizer:=x1}") == "tokenizer:=[REDACTED]}"
