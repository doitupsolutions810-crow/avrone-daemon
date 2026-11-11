"""Deterministic host and port perturbations for Fusion boundary checks."""
import random
from avrone.fusion_loopback import origin_key, is_access_loopback, is_any_loopback

def test_fuzz_malformed_ports_fail_closed():
    rng = random.Random(1010)
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz-+:.[]％８０!"
    for _ in range(2000):
        raw = "".join(rng.choices(alphabet, k=rng.randint(1, 12)))
        result = origin_key("http://127.0.0.1:" + raw)
        assert isinstance(result, str)
        if not (raw.isascii() and raw.isdecimal() and int(raw) <= 65535):
            assert result == "invalid:"

def test_fuzz_invalid_brackets_are_not_loopback():
    rng = random.Random(1020)
    prefixes = ["[", "[[", "x[", "[x", "]", "][", ""]
    suffixes = ["]", "]]", "]x", "x]", "[", "][", ""]
    for _ in range(1500):
        ip = rng.choice(["::1", "127.0.0.1", "localhost", "::ffff:127.0.0.1"])
        prefix = rng.choice(prefixes)
        suffix = rng.choice(suffixes)
        host = prefix + ip + suffix
        valid = (not prefix and not suffix) or (prefix == "[" and suffix == "]" and ":" in ip)
        if not valid:
            assert not is_access_loopback(host), repr(host)
            assert not is_any_loopback(host), repr(host)
