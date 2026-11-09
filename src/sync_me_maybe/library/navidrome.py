"""Ask Navidrome to rescan after new files are stored.

Uses the Subsonic API (``startScan``) with token authentication. Requests are
debounced so a 300-track playlist triggers one scan shortly after the last
file instead of 300 scans. Navidrome's own file watcher still works without
this; the API call just makes new songs show up sooner and reliably.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

CLIENT_NAME = "sync-me-maybe"
API_VERSION = "1.16.1"


class NavidromeError(RuntimeError):
    pass


class NavidromeClient:
    """Minimal Subsonic client for ``ping`` and ``startScan``."""

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        *,
        debounce_seconds: float = 15.0,
        timeout_seconds: int = 15,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.debounce_seconds = debounce_seconds
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self._scan_task: asyncio.Task[None] | None = None

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        """Call a Subsonic endpoint and return the ``subsonic-response`` body. Blocking."""
        salt = secrets.token_hex(8)
        # The Subsonic API defines token auth as md5(password + salt).
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        query = {
            "u": self.user,
            "t": token,
            "s": salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
            **params,
        }
        response = self.session.get(
            f"{self.url}/rest/{method}", params=query, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        body = response.json().get("subsonic-response", {})
        if body.get("status") != "ok":
            error = body.get("error") or {}
            raise NavidromeError(error.get("message") or f"{method} failed")
        return dict(body)

    def ping(self) -> bool:
        try:
            self.call("ping")
        except (requests.RequestException, ValueError, NavidromeError):
            LOGGER.warning("Navidrome ping failed", exc_info=True)
            return False
        return True

    def start_scan(self) -> None:
        self.call("startScan")
        LOGGER.info("Requested Navidrome library scan")

    def request_scan(self) -> None:
        """Schedule one scan after a quiet period; repeated calls push it back."""
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
        self._scan_task = asyncio.create_task(self._scan_later())

    async def _scan_later(self) -> None:
        try:
            await asyncio.sleep(self.debounce_seconds)
        except asyncio.CancelledError:
            return
        try:
            await asyncio.to_thread(self.start_scan)
        except Exception:  # noqa: BLE001 - a missed scan only delays visibility.
            LOGGER.warning("Navidrome scan request failed", exc_info=True)

    async def flush(self) -> None:
        """Run a pending scan right away (used during shutdown)."""
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            self._scan_task = None
            try:
                await asyncio.to_thread(self.start_scan)
            except Exception:  # noqa: BLE001 - shutdown must not fail on Navidrome.
                LOGGER.warning("Navidrome scan request failed", exc_info=True)
