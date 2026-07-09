import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional


DEFAULT_HISTORY_CONFIG: Dict[str, object] = {
    "enabled": True,
    "sample_seconds": 300
}


class HistoryService:
    PERIODS = {
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "12m": timedelta(days=365)
    }

    def __init__(
        self,
        db_path,
        snapshot_provider: Callable[[], Dict[str, object]],
        sample_seconds=300,
        enabled=True,
        log_callback: Optional[Callable] = None
    ):
        self.db_path = Path(db_path)
        self.snapshot_provider = snapshot_provider
        self.sample_seconds = max(30, int(sample_seconds or 300))
        self.enabled = bool(enabled)
        self.log_callback = log_callback
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self._init_db()

    def start(self):
        if not self.enabled:
            self._log("HistoryService disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log("HistoryService started")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                self._log(f"HistoryService sampling failed: {exc}")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0, self.sample_seconds - elapsed))

    def poll_once(self):
        snapshot = self.snapshot_provider() or {}
        sample_time = datetime.now().isoformat(timespec="seconds")
        rows = self._snapshot_rows(snapshot, sample_time)
        if not rows:
            return 0
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO metric_samples (timestamp, location, metric, value, unit, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows
            )
        return len(rows)

    def statistics(self, period="24h", location="opicina"):
        locations = self._locations_for_filter(location)
        period_start = self._period_start(period)
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        return {
            "period": period,
            "location": location,
            "locations": locations,
            "metrics": {
                metric: {
                    "today": self._stats_for(metric, locations, today_start),
                    "period": self._stats_for(metric, locations, period_start)
                }
                for metric in ("temperature", "humidity")
            },
            "by_location": {
                loc: {
                    metric: {
                        "today": self._stats_for(metric, [loc], today_start),
                        "period": self._stats_for(metric, [loc], period_start)
                    }
                    for metric in ("temperature", "humidity")
                }
                for loc in locations
            }
        }

    def graph_data(self, period="24h", location="opicina", metric="temperature"):
        locations = self._locations_for_filter(location)
        start = self._period_start(period)
        placeholders = ",".join("?" for _ in locations)
        params = [metric, start, *locations]
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, location, value, unit
                FROM metric_samples
                WHERE metric = ? AND timestamp >= ? AND location IN ({placeholders})
                ORDER BY timestamp ASC
                """,
                params
            ).fetchall()
        series = {loc: [] for loc in locations}
        units = {}
        for row in rows:
            series[row["location"]].append({
                "timestamp": row["timestamp"],
                "value": row["value"]
            })
            units[row["location"]] = row["unit"] or ""
        return {
            "period": period,
            "location": location,
            "metric": metric,
            "unit": next((unit for unit in units.values() if unit), ""),
            "series": [
                {
                    "location": loc,
                    "label": self._location_label(loc),
                    "unit": units.get(loc, ""),
                    "points": points
                }
                for loc, points in series.items()
            ]
        }

    def snapshot(self, period="24h", location="opicina"):
        return {
            "statistics": self.statistics(period, location),
            "graph": self.graph_data(period, location, "temperature"),
            "sample_seconds": self.sample_seconds
        }

    def _snapshot_rows(self, snapshot, sample_time):
        rows = []
        locations = snapshot.get("locations") or {}
        for location_key, location in locations.items():
            for metric_key, metric in (location.get("metrics") or {}).items():
                if metric.get("placeholder") or not metric.get("online"):
                    continue
                value = self._number(metric.get("value"))
                if value is None:
                    continue
                rows.append((
                    sample_time,
                    location_key,
                    metric_key,
                    value,
                    metric.get("unit", ""),
                    metric.get("source", "")
                ))
        return rows

    def _stats_for(self, metric, locations: Iterable[str], start):
        locations = list(locations)
        if not locations:
            return {"min": None, "max": None}
        placeholders = ",".join("?" for _ in locations)
        params = [metric, start, *locations]
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT MIN(value) AS min_value, MAX(value) AS max_value
                FROM metric_samples
                WHERE metric = ? AND timestamp >= ? AND location IN ({placeholders})
                """,
                params
            ).fetchone()
        return {
            "min": self._round(row["min_value"] if row else None),
            "max": self._round(row["max_value"] if row else None)
        }

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metric_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    location TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_metric_samples_lookup
                ON metric_samples (metric, location, timestamp)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_metric_samples_timestamp
                ON metric_samples (timestamp)
                """
            )

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _period_start(self, period):
        delta = self.PERIODS.get(period, self.PERIODS["24h"])
        return (datetime.now() - delta).isoformat(timespec="seconds")

    def _locations_for_filter(self, location) -> List[str]:
        if location == "compare":
            return ["opicina", "cesclans"]
        if location in ("opicina", "cesclans"):
            return [location]
        return ["opicina"]

    def _location_label(self, location):
        return {
            "opicina": "Opicina",
            "cesclans": "Cesclans"
        }.get(location, location)

    def _log(self, message):
        if self.log_callback:
            self.log_callback(message)
        else:
            print(message)

    @staticmethod
    def _number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _round(value):
        return None if value is None else round(float(value), 1)
