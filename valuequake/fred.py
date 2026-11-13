from datetime import date

import httpx

from valuequake.clients import get_json

FRED_URL = "https://api.stlouisfed.org/fred"


def fetch_series(client: httpx.Client, series_id: str, start: date, end: date) -> list[dict]:
    """Daily observations between start and end inclusive; FRED marks missing days with '.', which are skipped."""
    body = get_json(client, f"{FRED_URL}/series/observations", {
        "series_id": series_id,
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "file_type": "json",
    })
    return [
        {"series_id": series_id, "date": date.fromisoformat(o["date"]), "value": float(o["value"])}
        for o in body["observations"] if o["value"] != "."
    ]


def fetch_release_dates(client: httpx.Client, release_id: int, start: date, end: date) -> list[date]:
    """Release dates between start and end inclusive, including scheduled future ones."""
    body = get_json(client, f"{FRED_URL}/release/dates", {
        "release_id": release_id,
        "realtime_start": start.isoformat(),
        "realtime_end": "9999-12-31",  # FRED's open-ended upper bound; future dates are filtered below
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
        "limit": 10000,
        "file_type": "json",
    })
    return sorted({d for r in body["release_dates"] if start <= (d := date.fromisoformat(r["date"])) <= end})
