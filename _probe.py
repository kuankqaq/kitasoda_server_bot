"""
Standalone probe for the KitaSoda SSE endpoint.

- Bypasses system proxies (trust_env=False, empty proxies={}).
- Uses curl_cffi with chrome impersonation (TLS/JA3/H2 fingerprint of real Chrome).
- Falls back to httpx with browser-shaped headers if curl_cffi missing.
- Reads up to N events or T seconds whichever first, then exits.

Run:
    python -m plugins.kitcoda._probe
or
    python E:\kuank\project\bot\plugins\kitcoda\_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import importlib.util
from pathlib import Path

THIS = Path(__file__).resolve().parent
SSE_PY = THIS / "sse_client.py"

# Defensively unset any system proxy env vars for this process so we are sure
# the request really goes direct.
for var in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
):
    os.environ.pop(var, None)

# Stub nonebot.log so sse_client imports cleanly outside of nonebot runtime.
import types

class _StubLogger:
    def info(self, *a, **k): print("[INFO]", *a)
    def warning(self, *a, **k): print("[WARN]", *a)
    def error(self, *a, **k): print("[ERROR]", *a)
    def debug(self, *a, **k): pass

sys.modules.setdefault("nonebot", types.ModuleType("nonebot"))
nonebot_log_mod = types.ModuleType("nonebot.log")
nonebot_log_mod.logger = _StubLogger()
sys.modules["nonebot.log"] = nonebot_log_mod

# Load sse_client.py without triggering the package's __init__.py
spec = importlib.util.spec_from_file_location("kitcoda_sse_client", SSE_PY)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


async def main(max_events: int = 8, deadline_s: float = 25.0) -> int:
    print(f"=== probe start: max_events={max_events}, deadline={deadline_s}s ===")
    print(f"URL = {mod.SSE_URL}")
    try:
        import curl_cffi
        print(f"curl_cffi version: {curl_cffi.__version__}")
    except ImportError:
        print("curl_cffi: NOT INSTALLED — will fall back to httpx")

    # ---- Phase 0: sanity-test the host with a plain GET -------------------
    print("\n--- Phase 0: plain GET https://api.kitasoda.com/ ---")
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(
            impersonate="chrome",
            trust_env=False,
            proxies={},
            timeout=10,
        ) as s:
            r = await s.get("https://api.kitasoda.com/", allow_redirects=True)
            body = r.text
            print(f"  status={r.status_code} len={len(body)} server={r.headers.get('server')} cf-ray={r.headers.get('cf-ray')}")
            print(f"  body[:200]={body[:200]!r}")
    except Exception as e:
        print(f"  phase0 error: {e!r}")

    print("\n--- Phase 1: SSE stream ---")
    store = mod.ServerStore()

    # Wrap _handle_event to count + show events.
    orig_handle = store._handle_event
    counter = {"n": 0}

    def spy(ev):
        counter["n"] += 1
        name = ev.get("event")
        payload = ev.get("payload") or {}
        if isinstance(payload, dict):
            if payload.get("initial"):
                print(f"  [{counter['n']:>3}] initial dump, {len(payload.get('serverDetailAll') or [])} servers")
            elif payload.get("serverDetail"):
                s = payload["serverDetail"]
                print(f"  [{counter['n']:>3}] update id={s.get('id')} status={s.get('status')} num={s.get('numPlayers')}")
            elif name == "serverDetailRoundComplete":
                print(f"  [{counter['n']:>3}] round complete, {payload.get('total')} total / {payload.get('success')} ok")
            else:
                print(f"  [{counter['n']:>3}] {name} {sorted(payload.keys())}")
        orig_handle(ev)

    store._handle_event = spy

    started = time.monotonic()
    store.start()

    # Wait until either first frame received OR deadline hit.
    try:
        while True:
            await asyncio.sleep(0.5)
            elapsed = time.monotonic() - started
            if counter["n"] >= max_events:
                print(f"--- got {counter['n']} events in {elapsed:.1f}s, stopping ---")
                break
            if elapsed > deadline_s:
                print(f"--- {elapsed:.1f}s elapsed, stopping (events={counter['n']}) ---")
                break
    finally:
        await store.stop()

    print(f"=== probe done: ready={store.ready}, servers cached={len(store.all())} ===")
    return 0 if store.ready else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
