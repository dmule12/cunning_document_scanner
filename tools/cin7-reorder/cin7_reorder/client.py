"""HTTP client for the Cin7 Core API.

Handles the three things that will otherwise bite a scheduled job:

* **Rate limits.** Cin7 allows 3 calls/second, 60/minute and 5000/day,
  answering 429 with a ``Retry-After`` header. A token bucket keeps us under
  the per-second and per-minute limits proactively; 429s are retried with
  backoff; and a call budget aborts the run before it can exhaust the daily
  allowance and lock out everything else using the same credentials.

* **Paging.** Bulk endpoints default to 100 records and cap at 500.

* **Nothing writes unless asked.** ``post`` and ``put`` refuse to send when
  the client is in read-only mode, which is the default.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import httpx

from .config import ApiConfig, Credentials
from .schema import extract_list

log = logging.getLogger(__name__)

# Cin7's documented limits.
CALLS_PER_SECOND = 3
CALLS_PER_MINUTE = 60
MAX_RETRIES = 5


class Cin7Error(RuntimeError):
    """An API call failed in a way the run cannot recover from."""


class CallBudgetExceeded(Cin7Error):
    """The run hit its configured call budget and stopped deliberately."""


class ReadOnlyViolation(Cin7Error):
    """A write was attempted while the client is in read-only mode.

    This is a programming error, not a runtime condition: it means a code
    path that should be read-only tried to mutate the account.
    """


@dataclass
class ProbeResult:
    """Outcome of a single candidate-endpoint request."""

    path: str
    ok: bool
    status: Optional[int] = None
    payload: Any = None
    detail: str = ""


@dataclass
class _RateLimiter:
    """Token bucket honouring both the per-second and per-minute limits."""

    per_second: int = CALLS_PER_SECOND
    per_minute: int = CALLS_PER_MINUTE

    def __post_init__(self) -> None:
        self._recent: deque[float] = deque()

    def acquire(self, *, sleep=time.sleep, now=time.monotonic) -> None:
        while True:
            current = now()
            while self._recent and current - self._recent[0] >= 60.0:
                self._recent.popleft()

            in_last_second = sum(1 for t in self._recent if current - t < 1.0)

            if in_last_second >= self.per_second:
                oldest_in_second = next(
                    t for t in self._recent if current - t < 1.0
                )
                sleep(max(0.0, 1.0 - (current - oldest_in_second)) + 0.01)
                continue

            if len(self._recent) >= self.per_minute:
                sleep(max(0.0, 60.0 - (current - self._recent[0])) + 0.01)
                continue

            self._recent.append(current)
            return


class NullRateLimiter:
    """No-op limiter for tests against a mock transport.

    Never use this against the real API: it would burn through the per-second
    limit and get the run 429'd on almost every call.
    """

    def acquire(self, **_kwargs: object) -> None:
        return None


class Cin7Client:
    """Thin, careful wrapper over the Cin7 Core v2 API."""

    def __init__(
        self,
        credentials: Credentials,
        api_config: Optional[ApiConfig] = None,
        *,
        read_only: bool = True,
        transport: Optional[httpx.BaseTransport] = None,
        rate_limiter: Optional[object] = None,
    ) -> None:
        self.credentials = credentials
        self.config = api_config or ApiConfig()
        self.read_only = read_only
        self.call_count = 0

        self._limiter = rate_limiter or _RateLimiter()
        self._client = httpx.Client(
            base_url=credentials.base_url,
            headers={
                "api-auth-accountid": credentials.account_id,
                "api-auth-applicationkey": credentials.app_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self.config.timeout_seconds,
            transport=transport,
        )

    # -- endpoint discovery ------------------------------------------------

    def try_get(
        self, path: str, *, base_url: Optional[str] = None, **params: Any
    ) -> "ProbeResult":
        """A GET that reports what happened instead of raising.

        Used only by ``probe``, to test candidate endpoint paths without a
        single 404 aborting the whole investigation. Cin7 answers an unknown
        path with a 302 to an HTML error page rather than a clean 404, so
        "did this return JSON" is the only reliable success test.
        """
        url = path if base_url is None else f"{base_url.rstrip('/')}/{path}"

        self._limiter.acquire()
        self.call_count += 1

        try:
            response = self._client.get(url, params=_clean(params))
        except httpx.TransportError as exc:
            return ProbeResult(path=url, ok=False, detail=f"transport error: {exc}")

        if response.status_code >= 400:
            return ProbeResult(
                path=url, ok=False, status=response.status_code, detail="HTTP error"
            )

        try:
            payload = response.json()
        except ValueError:
            return ProbeResult(
                path=url,
                ok=False,
                status=response.status_code,
                detail="non-JSON body (Cin7 redirects unknown paths to an HTML error page)",
            )

        return ProbeResult(
            path=url, ok=True, status=response.status_code, payload=payload
        )

    @property
    def base_url_v1(self) -> str:
        """The v1 base, derived from the configured v2 base.

        Some resources exist only on the older API version, so the tool has
        to be able to reach both.
        """
        base = self.credentials.base_url.rstrip("/")
        if base.endswith("/v2"):
            return base[: -len("/v2")]
        return base

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Cin7Client":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- core request ------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> Any:
        if method.upper() not in {"GET", "HEAD"} and self.read_only:
            raise ReadOnlyViolation(
                f"{method} {path} attempted while the client is read-only. "
                "Writes require constructing the client with read_only=False, "
                "which the CLI only does when --apply is passed."
            )

        if self.call_count >= self.config.daily_call_budget:
            raise CallBudgetExceeded(
                f"Reached the configured call budget of "
                f"{self.config.daily_call_budget}. Stopping before Cin7's "
                "5000/day limit is exhausted. Raise `api.daily_call_budget` "
                "in config.yaml if this is expected."
            )

        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            self._limiter.acquire()
            self.call_count += 1

            try:
                response = self._client.request(
                    method, path, params=params, json=json
                )
            except httpx.TransportError as exc:
                # Network blips are worth retrying; a broken DNS name is not,
                # but we cannot tell them apart, so bound the attempts.
                last_error = exc
                self._sleep_backoff(attempt)
                continue

            if response.status_code == 429:
                wait = self._retry_after_seconds(response, attempt)
                log.warning(
                    "Rate limited by Cin7 on %s %s; waiting %.1fs (attempt %d/%d)",
                    method,
                    path,
                    wait,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                last_error = Cin7Error(
                    f"{method} {path} returned {response.status_code}: "
                    f"{response.text[:500]}"
                )
                self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                raise Cin7Error(
                    f"{method} {path} returned {response.status_code}: "
                    f"{response.text[:2000]}"
                )

            if not response.content:
                return {}

            try:
                return response.json()
            except ValueError as exc:
                raise Cin7Error(
                    f"{method} {path} returned a non-JSON body: "
                    f"{response.text[:500]}"
                ) from exc

        raise Cin7Error(
            f"{method} {path} failed after {MAX_RETRIES} attempts."
        ) from last_error

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(2**attempt, 30))

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                pass
        return float(min(2**attempt, 60))

    # -- verbs -------------------------------------------------------------

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=_clean(params))

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", path, json=payload)

    def put(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("PUT", path, json=payload)

    def delete(self, path: str, **params: Any) -> Any:
        return self.request("DELETE", path, params=_clean(params))

    # -- paging ------------------------------------------------------------

    def paginate(self, path: str, **params: Any) -> Iterator[dict]:
        """Yield every record from a paged bulk endpoint.

        Stops when a page comes back short or empty. A hard page ceiling
        guards against an endpoint that ignores ``page`` and would otherwise
        loop until the call budget runs out.
        """
        page = 1
        limit = self.config.page_size
        max_pages = max(1, self.config.daily_call_budget)

        while page <= max_pages:
            payload = self.get(path, page=page, limit=limit, **params)
            records = extract_list(payload)

            if not records:
                return

            yield from records

            if len(records) < limit:
                return

            page += 1


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}
