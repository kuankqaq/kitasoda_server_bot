"""
KitaSoda SSE client — maintains an in-memory map of L4D2 servers.

Uses curl_cffi.AsyncSession with a Chrome TLS/JA3 impersonation profile so
the upstream's TLS fingerprint filter (the very thing that was closing our
plain httpx connection) cannot tell us apart from a real browser.

If curl_cffi is not installed, falls back to plain httpx with browser-shaped
headers — same code path as before — so the plugin is never DOA.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Optional

from nonebot.log import logger

SSE_URL = "https://api.kitasoda.com/subscribeServerDetailAll"
DEFAULT_RETRY_MS = 2000
MAX_BACKOFF_MS = 30_000

# Optional outbound proxy. Set to "http://host:port" or "socks5://host:port"
# (or a dict {"http": "...", "https": "..."}) when the upstream IP-blocks your
# direct egress. Leave None to go straight.
# socks5h:// resolves DNS through the proxy too — preferred for region locks.
PROXY: Optional[str] = None

# Pretend to be a recent desktop Chrome connecting from the official frontend.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/event-stream",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # Real Chrome advertises these; curl_cffi handles decompression for us.
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://kitasoda.com",
    "Referer": "https://kitasoda.com/",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# curl_cffi impersonation profile.  "chrome" tracks the latest stable Chrome
# JA3/Akamai fingerprint that the library ships.
IMPERSONATE_PROFILE = "chrome"


def _parse_block(block: str) -> Optional[dict]:
    """Parse a single SSE event block (lines separated by \\n, blocks by \\n\\n)."""
    event_name: Optional[str] = None
    data_lines: list[str] = []
    retry_ms: Optional[int] = None
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if v.startswith(" "):
            v = v[1:]
        if k == "event":
            event_name = v
        elif k == "data":
            data_lines.append(v)
        elif k == "retry":
            try:
                retry_ms = int(v)
            except ValueError:
                pass
    if not data_lines and retry_ms is None and event_name is None:
        return None
    payload: Any = None
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            payload = None
    return {"event": event_name, "payload": payload, "retry": retry_ms}


class ServerStore:
    """Coroutine-safe (single event loop) cache of the SSE world."""

    def __init__(self) -> None:
        self._servers: dict[int, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._ready = asyncio.Event()
        self._last_update: Optional[str] = None

    # ---------- public read API ----------

    def all(self) -> list[dict]:
        return sorted(self._servers.values(), key=lambda s: s.get("id", 0))

    def active(self) -> list[dict]:
        return [s for s in self._servers.values() if (s.get("numPlayers") or 0) > 0]

    def by_id(self, sid: int) -> Optional[dict]:
        return self._servers.get(sid)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def last_update(self) -> Optional[str]:
        return self._last_update

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="kitcoda-sse")
        logger.info("[kitcoda] SSE client task started")

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ---------- internals ----------

    async def _run(self) -> None:
        # Pick a transport:
        #   1. curl_cffi  (TLS impersonation — the right tool for fingerprint blocks)
        #   2. httpx      (fallback if curl_cffi missing)
        try:
            from curl_cffi.requests import AsyncSession  # noqa: F401
            transport = "curl_cffi"
        except ImportError:
            transport = "httpx"
            logger.warning(
                "[kitcoda] curl_cffi not installed — falling back to httpx. "
                "If the upstream is doing TLS fingerprint filtering this will "
                "still fail. Run `pip install curl_cffi` to enable Chrome "
                "fingerprint impersonation."
            )

        retry_ms = DEFAULT_RETRY_MS
        backoff_ms = DEFAULT_RETRY_MS

        while not self._stopped:
            connected = False
            try:
                if transport == "curl_cffi":
                    connected = await self._stream_curl_cffi()
                else:
                    connected = await self._stream_httpx()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[kitcoda] SSE error ({transport}): {e!r}")

            if self._stopped:
                break

            if connected:
                backoff_ms = retry_ms
            else:
                backoff_ms = min(int(backoff_ms * 2), MAX_BACKOFF_MS)
            sleep_ms = backoff_ms + random.randint(0, max(500, backoff_ms // 4))
            logger.info(f"[kitcoda] SSE reconnect in {sleep_ms}ms")
            try:
                await asyncio.sleep(sleep_ms / 1000)
            except asyncio.CancelledError:
                raise

    # ---- curl_cffi backend (Chrome JA3 / Akamai fingerprint) ----

    async def _stream_curl_cffi(self) -> bool:
        from curl_cffi.requests import AsyncSession

        connected = False
        # AsyncSession is itself an async context manager.
        # NOTE: We force HTTP/1.1.  The upstream cluster-worker resets HTTP/2
        # streams shortly after open (PROTOCOL_ERROR), even with a perfect
        # Chrome JA3 fingerprint.  Real browsers' EventSource also negotiates
        # H1.1 here, so this is both safer and more authentic.
        from curl_cffi.const import CurlHttpVersion
        async with AsyncSession(
            impersonate=IMPERSONATE_PROFILE,
            timeout=None,  # SSE is long-lived
            default_headers=False,  # let our BROWSER_HEADERS rule
            trust_env=False,  # do NOT pick up HTTP_PROXY/HTTPS_PROXY/etc.
            proxies={"http": PROXY, "https": PROXY} if PROXY else {},
            http_version=CurlHttpVersion.V1_1,
        ) as session:
            async with session.stream(
                "GET",
                SSE_URL,
                headers=BROWSER_HEADERS,
                allow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    snippet = ""
                    try:
                        body = await resp.acontent()
                        snippet = body[:512].decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning(
                        f"[kitcoda] SSE rejected: HTTP {resp.status_code} "
                        f"server={resp.headers.get('server')} "
                        f"cf-ray={resp.headers.get('cf-ray')} "
                        f"body={snippet!r}"
                    )
                    return False
                connected = True
                logger.info(
                    f"[kitcoda] SSE connected (curl_cffi/{IMPERSONATE_PROFILE}) "
                    f"geo={resp.headers.get('x-client-geo')} "
                    f"worker={resp.headers.get('x-worker')}"
                )
                buf = ""
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    if isinstance(chunk, bytes):
                        try:
                            chunk = chunk.decode("utf-8")
                        except UnicodeDecodeError:
                            chunk = chunk.decode("utf-8", errors="replace")
                    buf += chunk
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        ev = _parse_block(block)
                        if not ev:
                            continue
                        self._handle_event(ev)
        return connected

    # ---- httpx fallback ----

    async def _stream_httpx(self) -> bool:
        import httpx

        connected = False
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            trust_env=False,  # ignore system proxy env vars
            proxy=PROXY,
        ) as client:
            async with client.stream("GET", SSE_URL) as resp:
                if resp.status_code != 200:
                    try:
                        body = await resp.aread()
                        snippet = body[:512].decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        snippet = "<unreadable>"
                    logger.warning(
                        f"[kitcoda] SSE rejected: HTTP {resp.status_code} "
                        f"server={resp.headers.get('server')} "
                        f"cf-ray={resp.headers.get('cf-ray')} "
                        f"body={snippet!r}"
                    )
                    return False
                connected = True
                logger.info(
                    f"[kitcoda] SSE connected (httpx) "
                    f"geo={resp.headers.get('x-client-geo')} "
                    f"worker={resp.headers.get('x-worker')}"
                )
                buf = ""
                async for chunk in resp.aiter_text():
                    if not chunk:
                        continue
                    buf += chunk
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        ev = _parse_block(block)
                        if not ev:
                            continue
                        self._handle_event(ev)
        return connected

    # ---- shared event handling ----

    def _handle_event(self, ev: dict) -> None:
        event_name = ev.get("event")
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            return

        if event_name == "serverDetailUpdate":
            if (
                "initial" not in payload
                and "serverDetail" not in payload
                and "serverDetailAll" not in payload
                and payload.get("status")
                and payload.get("status") != "SUCCESS"
            ):
                logger.warning(f"[kitcoda] error frame: {payload!r}")
                return

            if payload.get("initial"):
                new_map: dict[int, dict] = {}
                for s in payload.get("serverDetailAll") or []:
                    if s.get("status") == "SUCCESS" and "id" in s:
                        new_map[s["id"]] = s
                self._servers = new_map
                self._last_update = payload.get("updatedAt")
                self._ready.set()
                logger.info(f"[kitcoda] initial dump: {len(self._servers)} servers")
            else:
                s = payload.get("serverDetail")
                if not isinstance(s, dict) or "id" not in s:
                    return
                sid = s["id"]
                if s.get("status") == "SUCCESS":
                    self._servers[sid] = s
                else:
                    self._servers.pop(sid, None)
                self._last_update = payload.get("updatedAt")

        elif event_name == "serverDetailRoundComplete":
            ids = payload.get("serverIds")
            if not isinstance(ids, list):
                return
            alive = set(ids)
            stale = [sid for sid in self._servers if sid not in alive]
            for sid in stale:
                self._servers.pop(sid, None)
            if stale:
                logger.debug(f"[kitcoda] round trim: removed {len(stale)} stale ids")


# module-level singleton
store = ServerStore()
