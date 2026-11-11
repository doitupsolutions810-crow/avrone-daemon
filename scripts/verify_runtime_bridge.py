#!/usr/bin/env python3
"""Run the AVRONE plugin against an actual local Runtime HTTP gateway.

The Runtime remains an independent service. Pass an unpacked, trusted Runtime
source directory; no remote credentials or production endpoint are used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def verify(runtime_source):
    sys.path.insert(0, str(runtime_source))
    from avrone_runtime import ApiGateway, Runtime
    from avrone.tools import ToolRegistry

    spec = importlib.util.spec_from_file_location(
        "avrone_runtime_bridge_verification", ROOT / "plugins/platform_bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    registry = ToolRegistry()
    bridge.register(registry)
    checks = []

    async def call(args):
        raw = await registry.get("platform_bridge_create_runtime_swarm").fn(None, args)
        if raw.startswith("ERROR:"):
            return {"ok": False, "error": raw}
        return json.loads(raw)

    def passed(name):
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix="avrone-bridge-") as directory:
        runtime = Runtime(str(Path(directory) / "runtime.db"))
        gateway = ApiGateway(runtime, rate_limit=10000).start()
        try:
            key = gateway.create_api_key("bridge-tenant-a", "local-verification")
            other_key = gateway.create_api_key("bridge-tenant-b", "local-verification")
            os.environ["AVRONE_PLATFORM_TARGETS_JSON"] = json.dumps(
                {"runtime": "http://127.0.0.1:" + str(gateway.port)}
            )
            os.environ["AVRONE_BRIDGE_RUNTIME_TOKEN"] = key
            os.environ["AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON"] = "[]"
            os.environ["AVRONE_PLATFORM_WRITE_ENABLED"] = "false"
            args = {
                "mission_name": "Compatibility mission",
                "objective": "Verify durable mission, task and swarm records",
                "swarm_name": "Compatibility squad",
                "idempotency_key": "local-bridge-check",
            }

            def counts():
                with runtime._state_lock:
                    snapshot = {
                        table: runtime.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                        for table in ("missions", "swarms")
                    }
                    snapshot["tasks"] = len(runtime.tasks)
                    return snapshot

            def exactly(expected):
                require(counts() == dict.fromkeys(("missions", "swarms", "tasks"), expected),
                        "unexpected durable record counts: " + repr(counts()))

            disabled = await call(args)
            require(disabled["ok"] is False and "disabled" in disabled["error"],
                    "write opt-in was not enforced")
            exactly(0)
            passed("writes disabled without side effects")

            os.environ["AVRONE_PLATFORM_WRITE_ENABLED"] = "true"
            health = json.loads(await bridge._request("runtime", "GET", "/v1/health"))
            require(health.get("ok") is True and health["data"].get("ok") is True,
                    "authenticated gateway health failed")
            passed("actual authenticated HTTP health")

            original = await call(args)
            require(original.get("ok") is True, "mission/task/swarm creation failed: " + repr(original))
            ids = {name: original[name] for name in ("mission_id", "task_id", "swarm_id")}
            require(await call(args) == original, "stable retry changed the returned record identities")
            exactly(1)
            passed("real mission/task/swarm creation and identical replay")

            conflict = await call({**args, "objective": "Changed objective"})
            require(conflict.get("ok") is False and conflict.get("phase") == "mission",
                    "changed payload replay was accepted")
            exactly(1)
            passed("changed payload rejected without duplicate records")

            os.environ["AVRONE_BRIDGE_RUNTIME_TOKEN"] = other_key
            isolated = json.loads(await registry.get("platform_bridge_runtime_mission_status").fn(
                None, {"mission_id": ids["mission_id"]}))
            require(isolated.get("ok") is False and isolated.get("http_status") == 403,
                    "another tenant could read the mission")
            tenant_b = await call(args)
            require(tenant_b.get("ok") is True and tenant_b["mission_id"] != ids["mission_id"],
                    "idempotency key namespace is not tenant-scoped")
            exactly(2)
            passed("tenant isolation and independent tenant replay namespace")
            os.environ["AVRONE_BRIDGE_RUNTIME_TOKEN"] = key

            concurrent_args = {**args, "idempotency_key": "local-concurrent"}
            burst = await asyncio.gather(*(call(concurrent_args) for _ in range(12)))
            require(any(item.get("ok") is True for item in burst), "all concurrent attempts failed")
            replay = await call(concurrent_args)
            require(replay.get("ok") is True, "post-concurrency replay failed")
            for item in burst:
                if item.get("ok"):
                    require(all(item[name] == replay[name] for name in ids),
                            "concurrent replay returned different records")
                else:
                    require(item.get("http_status") == 409,
                            "concurrent failure was not a pending reservation")
            exactly(3)
            passed("12 concurrent identical workflows create one record chain")

            real_request = bridge._request
            lost = {"once": False}
            async def lose_task_ack(target, method, path, body=None, idempotency_key=None):
                result = await real_request(target, method, path, body, idempotency_key=idempotency_key)
                if path == "/v1/tasks" and not lost["once"] and json.loads(result).get("ok"):
                    lost["once"] = True
                    return json.dumps({"ok": False, "transport_ambiguous": True,
                                       "error": "injected lost acknowledgement"})
                return result
            bridge._request = lose_task_ack
            partial_args = {**args, "idempotency_key": "local-lost-task-ack"}
            try:
                partial = await call(partial_args)
            finally:
                bridge._request = real_request
            require(partial.get("ok") is False and partial.get("phase") == "task"
                    and partial.get("partial") is True
                    and partial.get("transport_ambiguous") is True,
                    "lost acknowledgement did not report a recoverable partial result")
            recovered = await call(partial_args)
            require(recovered.get("ok") is True, "same-key recovery failed")
            exactly(4)
            passed("lost task acknowledgement recovered with the original key")

            gateway.shutdown()
            gateway = ApiGateway(runtime, rate_limit=10000).start()
            os.environ["AVRONE_PLATFORM_TARGETS_JSON"] = json.dumps(
                {"runtime": "http://127.0.0.1:" + str(gateway.port)}
            )
            require(await call(args) == original, "gateway restart lost original receipt identities")
            exactly(4)
            passed("gateway restart preserves replay identity")

            reconciliation = gateway.reconciliation_evidence()
            require(reconciliation.get("ok") is True, "gateway idempotency/audit reconciliation failed")
            require(runtime.audit_integrity().get("ok") is True, "runtime audit integrity failed")
            passed("durable records reconcile with the gateway audit")
            return {
                "schema": "avrone.runtime-bridge-verification.v1",
                "ok": True,
                "checks": checks,
                "counts": counts(),
                "concurrent_attempts": len(burst),
                "reconciliation": reconciliation,
                "sources_sha256": {
                    "platform_bridge.py": fingerprint(ROOT / "plugins/platform_bridge.py"),
                    "runtime/core.py": fingerprint(runtime_source / "avrone_runtime/core.py"),
                    "runtime/gateway.py": fingerprint(runtime_source / "avrone_runtime/gateway.py"),
                },
                "scope": "Actual local HTTP gateway and durable SQLite record orchestration; "
                         "does not execute external providers or dispatch agents.",
            }
        finally:
            gateway.shutdown()
            runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    try:
        source = options.runtime_source.resolve(strict=True)
        require((source / "avrone_runtime/gateway.py").is_file(),
                "runtime source must contain avrone_runtime/gateway.py")
        result = asyncio.run(verify(source))
    except Exception as error:
        result = {"schema": "avrone.runtime-bridge-verification.v1", "ok": False,
                  "error_type": type(error).__name__, "error": str(error)}
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
