import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class GardenLightTimerService:
    """Durable, single-relay staircase-light timer state machine."""

    CURRENT_STATES = ("ARMING", "ACTIVE", "OFF_PENDING", "RETRY_WAIT", "FAULT")
    TERMINAL_STATES = ("INACTIVE", "COMPLETED", "CANCELLED")
    RETRY_SECONDS = (15, 30, 60, 60, 60)
    LONG_RETRY_SECONDS = 300

    def __init__(
        self,
        db_path,
        house_id,
        device_id,
        relay_number,
        relay_setter,
        relay_reader,
        log_callback=None,
        worker_interval_seconds=2,
        clock=None,
    ):
        self.db_path = Path(db_path)
        self.house_id = str(house_id)
        self.device_id = str(device_id)
        self.relay_number = str(relay_number)
        self.relay_setter = relay_setter
        self.relay_reader = relay_reader
        self.log_callback = log_callback
        self.worker_interval_seconds = max(0.1, float(worker_interval_seconds))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._worker_started_at = None
        self._last_worker_tick = None
        self._last_worker_error = None
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS garden_light_timers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timer_id TEXT NOT NULL UNIQUE,
                    generation INTEGER NOT NULL,
                    house_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    relay_number TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'INACTIVE', 'ARMING', 'ACTIVE', 'OFF_PENDING',
                            'RETRY_WAIT', 'COMPLETED', 'CANCELLED', 'FAULT'
                        )
                    ),
                    requested_duration_seconds INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    deadline_at TEXT NULL,
                    off_attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_off_attempt_at TEXT NULL,
                    next_retry_at TEXT NULL,
                    last_error TEXT NULL,
                    cancellation_reason TEXT NULL,
                    relay_on_confirmed INTEGER NOT NULL DEFAULT 0,
                    relay_off_confirmed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NULL,
                    UNIQUE (house_id, device_id, relay_number, generation)
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_garden_light_current_timer
                ON garden_light_timers (house_id, device_id, relay_number)
                WHERE state IN ('ARMING', 'ACTIVE', 'OFF_PENDING', 'RETRY_WAIT', 'FAULT')
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_garden_light_actionable
                ON garden_light_timers (house_id, device_id, relay_number, state, deadline_at, next_retry_at)
                """
            )

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._worker_started_at = self._iso(self._now())
            self._thread = threading.Thread(
                target=self._worker,
                name="garden-light-timer",
                daemon=True,
            )
            self._thread.start()
            self._log("WORKER_STARTED", interval_seconds=self.worker_interval_seconds)
            return True

    def stop(self, timeout=5):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._log("WORKER_STOPPED", alive=bool(thread and thread.is_alive()))

    def health(self):
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "worker_started_at": self._worker_started_at,
            "last_worker_tick": self._last_worker_tick,
            "last_worker_error": self._last_worker_error,
        }

    def start_timer(self, duration_seconds):
        duration_seconds = int(duration_seconds)
        if duration_seconds < 1:
            raise ValueError("Duration must be positive")
        with self._lock:
            current_time = self._now()
            current_iso = self._iso(current_time)
            timer_id = uuid.uuid4().hex
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                generation = self._next_generation(conn)
                self._cancel_current_tx(conn, "SUPERSEDED", current_iso)
                conn.execute(
                    """
                    INSERT INTO garden_light_timers (
                        timer_id, generation, house_id, device_id, relay_number,
                        state, requested_duration_seconds, started_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'ARMING', ?, ?, ?, ?)
                    """,
                    (
                        timer_id, generation, self.house_id, self.device_id,
                        self.relay_number, duration_seconds, current_iso,
                        current_iso, current_iso,
                    ),
                )
            self._log("ARMING_PERSISTED", timer_id=timer_id, generation=generation, duration_seconds=duration_seconds)
            self._log("ON_COMMAND_SENT", timer_id=timer_id, generation=generation)
            status = self._safe_set(True)
            if self._confirmed(status, True):
                deadline = current_time + timedelta(seconds=duration_seconds)
                with self._connect() as conn:
                    changed = conn.execute(
                        """
                        UPDATE garden_light_timers
                        SET state='ACTIVE', deadline_at=?, relay_on_confirmed=1,
                            last_error=NULL, updated_at=?
                        WHERE timer_id=? AND generation=? AND state='ARMING'
                        """,
                        (self._iso(deadline), self._iso(self._now()), timer_id, generation),
                    ).rowcount
                if changed != 1:
                    self._log("STALE_ARMING_RESULT", timer_id=timer_id, generation=generation)
                    return {"ok": False, "error": "Timer was superseded while arming"}
                self._log("ACTIVE_DEADLINE_STORED", timer_id=timer_id, generation=generation, deadline_at=self._iso(deadline))
                return {"ok": True, **self.status(timer_id)}
            error = self._status_error(status, "Relay ON was not confirmed")
            physical_status = self._safe_read()
            if not self._confirmed(physical_status, False):
                self._schedule_retry(timer_id, generation, error, increment=False, immediate=True)
                self._log(
                    "ON_UNCONFIRMED_OFF_OBLIGATION_PRESERVED",
                    timer_id=timer_id, generation=generation, error=error,
                )
                return {"ok": False, "error": error, **self.status(timer_id)}
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE garden_light_timers
                    SET state='FAULT', last_error=?, updated_at=?
                    WHERE timer_id=? AND generation=? AND state='ARMING'
                    """,
                    (error, self._iso(self._now()), timer_id, generation),
                )
            self._log("FAULT", timer_id=timer_id, generation=generation, error=error)
            return {"ok": False, "error": error, **self.status(timer_id)}

    def import_legacy_deadline(self, deadline_at):
        """One-time migration from the former JSON deadline without sending ON."""
        deadline = self._parse(deadline_at)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._current_row(conn):
                return False
            current_time = self._now()
            current_iso = self._iso(current_time)
            generation = self._next_generation(conn)
            duration = max(1, int((deadline - current_time).total_seconds()))
            conn.execute(
                """
                INSERT INTO garden_light_timers (
                    timer_id, generation, house_id, device_id, relay_number,
                    state, requested_duration_seconds, started_at, deadline_at,
                    relay_on_confirmed, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, 0, ?, ?)
                """,
                (
                    uuid.uuid4().hex, generation, self.house_id, self.device_id,
                    self.relay_number, duration, current_iso, self._iso(deadline),
                    current_iso, current_iso,
                ),
            )
        self._log("LEGACY_TIMER_MIGRATED", generation=generation, deadline_at=self._iso(deadline))
        return True

    def cancel_current(self, reason="USER_CANCELLED"):
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._current_row(conn)
                if not row:
                    return {"ok": True, "cancelled": False, **self.status()}
                changed = conn.execute(
                    f"""
                    UPDATE garden_light_timers
                    SET state='CANCELLED', cancellation_reason=?, updated_at=?, completed_at=?
                    WHERE timer_id=? AND generation=? AND state IN ({self._state_placeholders()})
                    """,
                    (
                        reason, self._iso(self._now()), self._iso(self._now()),
                        row["timer_id"], row["generation"], *self.CURRENT_STATES,
                    ),
                ).rowcount
            self._log("TIMER_CANCELLED", timer_id=row["timer_id"], generation=row["generation"], reason=reason)
            return {"ok": True, "cancelled": changed == 1, **self.status(row["timer_id"])}

    def manual_on(self):
        with self._lock:
            self.cancel_current("MANUAL_ON")
            self._log("MANUAL_ON_COMMAND_SENT")
            status = self._safe_set(True)
            return {"ok": self._confirmed(status, True), "relay_status": status, **self.status()}

    def manual_off(self):
        with self._lock:
            self._log("MANUAL_OFF_COMMAND_SENT")
            status = self._safe_set(False)
            if self._confirmed(status, False):
                result = self.cancel_current("MANUAL_OFF")
                return {"ok": True, "relay_status": status, **result}
            error = self._status_error(status, "Manual OFF was not confirmed")
            row = self._get_current()
            if row:
                self._schedule_retry(row["timer_id"], row["generation"], error, increment=False)
            self._log("MANUAL_OFF_FAILED", error=error)
            return {"ok": False, "error": error, "relay_status": status, **self.status()}

    def status(self, timer_id=None):
        with self._connect() as conn:
            if timer_id:
                row = conn.execute(
                    "SELECT * FROM garden_light_timers WHERE timer_id=?",
                    (timer_id,),
                ).fetchone()
            else:
                row = self._current_row(conn)
                if not row:
                    row = conn.execute(
                        """
                        SELECT * FROM garden_light_timers
                        WHERE house_id=? AND device_id=? AND relay_number=?
                        ORDER BY generation DESC LIMIT 1
                        """,
                        (self.house_id, self.device_id, self.relay_number),
                    ).fetchone()
        if not row:
            return {
                "timer_id": None, "generation": None, "state": "INACTIVE",
                "active": False, "deadline_at": None, "remaining_seconds": 0,
                "off_attempt_count": 0, "next_retry_at": None,
                "last_error": None, "relay_on_confirmed": False,
                "relay_off_confirmed": False, "data_quality": "authoritative",
                "server_time": self._iso(self._now()), "health": self.health(),
            }
        item = dict(row)
        remaining = 0
        if item.get("deadline_at"):
            remaining = max(0, int((self._parse(item["deadline_at"]) - self._now()).total_seconds()))
        return {
            "timer_id": item["timer_id"],
            "generation": item["generation"],
            "state": item["state"],
            "active": item["state"] in ("ARMING", "ACTIVE", "OFF_PENDING", "RETRY_WAIT"),
            "deadline_at": item["deadline_at"],
            "remaining_seconds": remaining,
            "off_attempt_count": item["off_attempt_count"],
            "next_retry_at": item["next_retry_at"],
            "last_error": item["last_error"],
            "relay_on_confirmed": bool(item["relay_on_confirmed"]),
            "relay_off_confirmed": bool(item["relay_off_confirmed"]),
            "cancellation_reason": item["cancellation_reason"],
            "data_quality": "authoritative",
            "server_time": self._iso(self._now()),
            "health": self.health(),
        }

    def process_once(self):
        with self._lock:
            self._last_worker_tick = self._iso(self._now())
            self._recover_arming()
            self._expire_active()
            self._retry_due()
            self._watchdog()

    def _worker(self):
        while not self._stop.is_set():
            try:
                self.process_once()
                self._last_worker_error = None
            except Exception as exc:
                self._last_worker_error = str(exc)
                self._log("WORKER_UNEXPECTED_EXCEPTION", error=str(exc))
            self._stop.wait(self.worker_interval_seconds)

    def _recover_arming(self):
        row = self._get_current("ARMING")
        if not row:
            return
        self._log("STARTUP_RECOVERY_ARMING", timer_id=row["timer_id"], generation=row["generation"])
        status = self._safe_read()
        current_iso = self._iso(self._now())
        if self._confirmed(status, True):
            deadline = self._parse(row["started_at"]) + timedelta(seconds=row["requested_duration_seconds"])
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE garden_light_timers
                    SET state='ACTIVE', deadline_at=?, relay_on_confirmed=1, updated_at=?
                    WHERE timer_id=? AND generation=? AND state='ARMING'
                    """,
                    (self._iso(deadline), current_iso, row["timer_id"], row["generation"]),
                )
            self._log("RECOVERY_ACTIVE", timer_id=row["timer_id"], generation=row["generation"], deadline_at=self._iso(deadline))
        elif self._confirmed(status, False):
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE garden_light_timers
                    SET state='CANCELLED', cancellation_reason='RECOVERY_RELAY_OFF',
                        completed_at=?, updated_at=?
                    WHERE timer_id=? AND generation=? AND state='ARMING'
                    """,
                    (current_iso, current_iso, row["timer_id"], row["generation"]),
                )
            self._log("RECOVERY_CANCELLED_RELAY_OFF", timer_id=row["timer_id"], generation=row["generation"])
        else:
            error = self._status_error(status, "Relay state unavailable during ARMING recovery")
            self._schedule_retry(row["timer_id"], row["generation"], error, increment=False, immediate=True)
            self._log("RECOVERY_OFF_OBLIGATION_PRESERVED", timer_id=row["timer_id"], generation=row["generation"], error=error)

    def _expire_active(self):
        current_iso = self._iso(self._now())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM garden_light_timers
                WHERE house_id=? AND device_id=? AND relay_number=?
                  AND state='ACTIVE' AND deadline_at IS NOT NULL AND deadline_at<=?
                ORDER BY generation DESC LIMIT 1
                """,
                (self.house_id, self.device_id, self.relay_number, current_iso),
            ).fetchone()
            if not row:
                return
            changed = conn.execute(
                """
                UPDATE garden_light_timers
                SET state='OFF_PENDING', updated_at=?
                WHERE timer_id=? AND generation=? AND state='ACTIVE'
                """,
                (current_iso, row["timer_id"], row["generation"]),
            ).rowcount
        if changed == 1:
            self._log("EXPIRATION_DETECTED", timer_id=row["timer_id"], generation=row["generation"])
            self._attempt_off(row["timer_id"], row["generation"])

    def _retry_due(self):
        current_iso = self._iso(self._now())
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timer_id, generation FROM garden_light_timers
                WHERE house_id=? AND device_id=? AND relay_number=?
                  AND (
                    state='OFF_PENDING'
                    OR (state='RETRY_WAIT' AND (next_retry_at IS NULL OR next_retry_at<=?))
                  )
                ORDER BY generation DESC
                """,
                (self.house_id, self.device_id, self.relay_number, current_iso),
            ).fetchall()
        for row in rows:
            self._log("RETRY_EXECUTED", timer_id=row["timer_id"], generation=row["generation"])
            self._attempt_off(row["timer_id"], row["generation"])

    def _attempt_off(self, timer_id, generation):
        attempt_time = self._now()
        with self._connect() as conn:
            changed = conn.execute(
                """
                UPDATE garden_light_timers
                SET state='OFF_PENDING', off_attempt_count=off_attempt_count+1,
                    last_off_attempt_at=?, next_retry_at=NULL, updated_at=?
                WHERE timer_id=? AND generation=?
                  AND state IN ('OFF_PENDING', 'RETRY_WAIT')
                """,
                (self._iso(attempt_time), self._iso(attempt_time), timer_id, generation),
            ).rowcount
            row = conn.execute(
                "SELECT off_attempt_count FROM garden_light_timers WHERE timer_id=? AND generation=?",
                (timer_id, generation),
            ).fetchone()
        if changed != 1:
            self._log("STALE_OFF_ATTEMPT_IGNORED", timer_id=timer_id, generation=generation)
            return
        attempt = int(row["off_attempt_count"])
        self._log("OFF_COMMAND_ATTEMPT", timer_id=timer_id, generation=generation, attempt=attempt)
        status = self._safe_set(False)
        if self._confirmed(status, False):
            completed = self._iso(self._now())
            with self._connect() as conn:
                changed = conn.execute(
                    """
                    UPDATE garden_light_timers
                    SET state='COMPLETED', relay_off_confirmed=1, last_error=NULL,
                        next_retry_at=NULL, completed_at=?, updated_at=?
                    WHERE timer_id=? AND generation=? AND state='OFF_PENDING'
                    """,
                    (completed, completed, timer_id, generation),
                ).rowcount
            if changed == 1:
                self._log("OFF_CONFIRMED", timer_id=timer_id, generation=generation, attempt=attempt)
            return
        error = self._status_error(status, "Relay OFF was not confirmed")
        self._schedule_retry(timer_id, generation, error, increment=False)

    def _schedule_retry(self, timer_id, generation, error, increment=False, immediate=False):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT off_attempt_count FROM garden_light_timers WHERE timer_id=? AND generation=?",
                (timer_id, generation),
            ).fetchone()
            if not row:
                return
            attempts = int(row["off_attempt_count"]) + (1 if increment else 0)
            delay = 0 if immediate else self._retry_delay(attempts)
            next_retry = self._now() + timedelta(seconds=delay)
            changed = conn.execute(
                f"""
                UPDATE garden_light_timers
                SET state='RETRY_WAIT', off_attempt_count=?,
                    next_retry_at=?, last_error=?, updated_at=?
                WHERE timer_id=? AND generation=? AND state IN ({self._state_placeholders()})
                """,
                (
                    attempts, self._iso(next_retry), error, self._iso(self._now()),
                    timer_id, generation, *self.CURRENT_STATES,
                ),
            ).rowcount
        if changed == 1:
            self._log(
                "RETRY_SCHEDULED", timer_id=timer_id, generation=generation,
                attempt=attempts, next_retry_at=self._iso(next_retry), error=error,
            )

    def _watchdog(self):
        current_iso = self._iso(self._now())
        with self._connect() as conn:
            duplicates = conn.execute(
                f"""
                SELECT COUNT(*) AS count FROM garden_light_timers
                WHERE house_id=? AND device_id=? AND relay_number=?
                  AND state IN ({self._state_placeholders()})
                """,
                (self.house_id, self.device_id, self.relay_number, *self.CURRENT_STATES),
            ).fetchone()["count"]
            missing_deadline = conn.execute(
                """
                SELECT timer_id, generation FROM garden_light_timers
                WHERE house_id=? AND device_id=? AND relay_number=?
                  AND state='ACTIVE' AND deadline_at IS NULL
                LIMIT 1
                """,
                (self.house_id, self.device_id, self.relay_number),
            ).fetchone()
            overdue_retry = conn.execute(
                """
                SELECT timer_id, generation FROM garden_light_timers
                WHERE house_id=? AND device_id=? AND relay_number=?
                  AND state='RETRY_WAIT' AND next_retry_at<?
                LIMIT 1
                """,
                (self.house_id, self.device_id, self.relay_number, current_iso),
            ).fetchone()
        if duplicates > 1:
            self._log("WATCHDOG_MULTIPLE_CURRENT_TIMERS", count=duplicates)
        if missing_deadline:
            self._log("WATCHDOG_ACTIVE_MISSING_DEADLINE", timer_id=missing_deadline["timer_id"])
            self._schedule_retry(missing_deadline["timer_id"], missing_deadline["generation"], "ACTIVE timer missing deadline", immediate=True)
        if overdue_retry:
            self._log("WATCHDOG_OVERDUE_RETRY", timer_id=overdue_retry["timer_id"])

    def _get_current(self, state=None):
        with self._connect() as conn:
            return self._current_row(conn, state)

    def _current_row(self, conn, state=None):
        states = (state,) if state else self.CURRENT_STATES
        placeholders = ",".join("?" for _ in states)
        return conn.execute(
            f"""
            SELECT * FROM garden_light_timers
            WHERE house_id=? AND device_id=? AND relay_number=?
              AND state IN ({placeholders})
            ORDER BY generation DESC LIMIT 1
            """,
            (self.house_id, self.device_id, self.relay_number, *states),
        ).fetchone()

    def _next_generation(self, conn):
        row = conn.execute(
            """
            SELECT MAX(generation) AS generation FROM garden_light_timers
            WHERE house_id=? AND device_id=? AND relay_number=?
            """,
            (self.house_id, self.device_id, self.relay_number),
        ).fetchone()
        return int(row["generation"] or 0) + 1

    def _cancel_current_tx(self, conn, reason, timestamp):
        row = self._current_row(conn)
        if not row:
            return
        conn.execute(
            f"""
            UPDATE garden_light_timers
            SET state='CANCELLED', cancellation_reason=?, completed_at=?, updated_at=?
            WHERE timer_id=? AND generation=? AND state IN ({self._state_placeholders()})
            """,
            (reason, timestamp, timestamp, row["timer_id"], row["generation"], *self.CURRENT_STATES),
        )
        self._log("TIMER_SUPERSEDED", timer_id=row["timer_id"], generation=row["generation"])

    def _safe_set(self, active):
        try:
            return self.relay_setter(bool(active)) or {}
        except Exception as exc:
            self._log("RELAY_COMMAND_EXCEPTION", active=bool(active), error=str(exc))
            return {"ok": False, "active": None, "error": str(exc)}

    def _safe_read(self):
        try:
            return self.relay_reader() or {}
        except Exception as exc:
            self._log("RELAY_READ_EXCEPTION", error=str(exc))
            return {"ok": False, "active": None, "error": str(exc)}

    @staticmethod
    def _confirmed(status, expected):
        return bool(status.get("ok") and status.get("active") is expected)

    @staticmethod
    def _status_error(status, fallback):
        return str(status.get("command_error") or status.get("error") or fallback)

    def _retry_delay(self, attempts):
        if attempts <= 0:
            return self.RETRY_SECONDS[0]
        if attempts <= len(self.RETRY_SECONDS):
            return self.RETRY_SECONDS[attempts - 1]
        return self.LONG_RETRY_SECONDS

    def _state_placeholders(self):
        return ",".join("?" for _ in self.CURRENT_STATES)

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _iso(value):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _parse(value):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)

    def _log(self, event, **fields):
        if self.log_callback:
            details = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
            self.log_callback(f"GARDEN_TIMER event={event}{' ' + details if details else ''}")
