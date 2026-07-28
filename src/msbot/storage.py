"""SQLite history store.

Every run appends a snapshot, so price trends over time are queryable later
without re-scraping. One row per (offer, run).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable, List, Optional

from .models import FlightOffer, ScrapeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    scraped_at      TEXT NOT NULL,
    source          TEXT NOT NULL,
    route           TEXT NOT NULL,
    search_date     TEXT NOT NULL,
    offer_key       TEXT NOT NULL,
    price_rial      INTEGER NOT NULL,
    airline_code    TEXT,
    airline_name    TEXT,
    flight_number   TEXT,
    cabin           TEXT,
    cabin_raw       TEXT,
    origin          TEXT,
    destination     TEXT,
    departure_time  TEXT,
    arrival_time    TEXT,
    duration_min    INTEGER,
    stops           INTEGER,
    seats_available INTEGER,
    baggage         TEXT,
    fare_type       TEXT,
    is_charter      INTEGER,
    deeplink        TEXT
);

CREATE INDEX IF NOT EXISTS idx_offers_lookup ON offers(route, search_date, source, cabin);
CREATE INDEX IF NOT EXISTS idx_offers_key    ON offers(offer_key, scraped_at);

CREATE TABLE IF NOT EXISTS scrape_log (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    source      TEXT NOT NULL,
    route       TEXT NOT NULL,
    search_date TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    n_offers    INTEGER NOT NULL,
    elapsed_s   REAL,
    error       TEXT
);
"""

_COLUMNS = [
    "run_id", "scraped_at", "source", "route", "search_date", "offer_key", "price_rial",
    "airline_code", "airline_name", "flight_number", "cabin", "cabin_raw", "origin",
    "destination", "departure_time", "arrival_time", "duration_min", "stops",
    "seats_available", "baggage", "fare_type", "is_charter", "deeplink",
]


class Storage:
    def __init__(self, path: str = "data/history.sqlite") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._conn()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def start_run(self, note: Optional[str] = None) -> int:
        from datetime import datetime

        with closing(self._conn()) as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, note) VALUES (?, ?)",
                (datetime.utcnow().isoformat(timespec="seconds") + "Z", note),
            )
            conn.commit()
            return int(cur.lastrowid)

    def save_offers(self, run_id: int, offers: Iterable[FlightOffer]) -> int:
        rows = []
        for o in offers:
            rows.append(
                (
                    run_id, o.scraped_at, o.source, o.route, o.search_date, o.offer_key(),
                    o.price_rial, o.airline_code, o.airline_name, o.flight_number, o.cabin,
                    o.cabin_raw, o.origin, o.destination, o.departure_time, o.arrival_time,
                    o.duration_min, o.stops, o.seats_available, o.baggage, o.fare_type,
                    int(o.is_charter) if o.is_charter is not None else None, o.deeplink,
                )
            )
        if not rows:
            return 0
        placeholders = ",".join("?" * len(_COLUMNS))
        sql = "INSERT INTO offers ({}) VALUES ({})".format(",".join(_COLUMNS), placeholders)
        with closing(self._conn()) as conn:
            conn.executemany(sql, rows)
            conn.commit()
        return len(rows)

    def log_result(self, run_id: int, result: ScrapeResult) -> None:
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO scrape_log (run_id, source, route, search_date, ok, n_offers, elapsed_s, error)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id, result.source, result.route, result.search_date,
                    int(result.ok), len(result.offers), result.elapsed_s, result.error,
                ),
            )
            conn.commit()

    def price_history(self, route: str, search_date: str, source: Optional[str] = None) -> List[sqlite3.Row]:
        sql = (
            "SELECT scraped_at, source, cabin, MIN(price_rial) AS min_price FROM offers"
            " WHERE route = ? AND search_date = ?"
        )
        params: List[object] = [route, search_date]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " GROUP BY scraped_at, source, cabin ORDER BY scraped_at"
        with closing(self._conn()) as conn:
            return list(conn.execute(sql, params))
