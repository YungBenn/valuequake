import os
import time as clock

import httpx

FRED_HOST = "api.stlouisfed.org"


class _ApiKeys(httpx.Auth):
    """Attach each provider's key only to that provider's host, so one client can call both safely."""

    def __init__(self) -> None:
        self.alpaca = {
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_API_SECRET_KEY"],
        }
        self.fred = os.environ["FRED_API_KEY"]

    def auth_flow(self, request: httpx.Request):
        if request.url.host.endswith(".alpaca.markets"):
            request.headers.update(self.alpaca)
        elif request.url.host == FRED_HOST:
            request.url = request.url.copy_merge_params({"api_key": self.fred})
        yield request


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """HTTP client for Alpaca (relative URLs) and FRED (absolute URLs), keys read from the environment."""
    return httpx.Client(base_url="https://data.alpaca.markets", auth=_ApiKeys(), transport=transport, timeout=30)


def get_json(client: httpx.Client, url: str, params: dict):
    """GET that stays under the plan's rate limit: waits for the window reset when it is used up or hit."""
    while True:
        resp = client.get(url, params=params)
        if resp.status_code == 429 or resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = float(resp.headers.get("X-RateLimit-Reset", clock.time() + 60))
            clock.sleep(max(reset - clock.time(), 0) + 0.1)
        if resp.status_code != 429:
            if resp.is_error:
                # Not raise_for_status: its message holds the full URL, and FRED's api_key is in the query string.
                safe_url = resp.request.url.copy_remove_param("api_key")
                raise httpx.HTTPStatusError(f"{resp.status_code} {resp.reason_phrase} for {safe_url}",
                                            request=resp.request, response=resp)
            return resp.json()
