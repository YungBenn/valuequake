import httpx
import pytest

from valuequake.clients import get_json, make_client


def test_each_key_goes_only_to_its_own_provider(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "alpaca-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "alpaca-secret")
    monkeypatch.setenv("FRED_API_KEY", "fred-key")
    sent = []
    client = make_client(httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={})))

    client.get("/v2/stocks/bars")
    client.get("https://paper-api.alpaca.markets/v2/calendar")
    client.get("https://api.stlouisfed.org/fred/series/observations", params={"series_id": "VIXCLS"})

    alpaca, trading, fred = sent
    for r in (alpaca, trading):
        assert r.headers["APCA-API-KEY-ID"] == "alpaca-id" and "api_key" not in r.url.params
    assert fred.url.params["api_key"] == "fred-key" and fred.url.params["series_id"] == "VIXCLS"
    assert "APCA-API-KEY-ID" not in fred.headers and "APCA-API-SECRET-KEY" not in fred.headers


def test_http_errors_do_not_carry_the_fred_key(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "alpaca-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "alpaca-secret")
    monkeypatch.setenv("FRED_API_KEY", "fred-key")
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(502)))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        get_json(client, "https://api.stlouisfed.org/fred/series/observations", {"series_id": "VIXCLS"})

    assert "502" in repr(exc_info.value) and "VIXCLS" in repr(exc_info.value)
    assert "fred-key" not in repr(exc_info.value)
