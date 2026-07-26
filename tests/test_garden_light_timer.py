import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from garden_light_timer_service import GardenLightTimerService


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeRelay:
    def __init__(self):
        self.active = False
        self.commands = []
        self.fail_on = False
        self.fail_off_count = 0
        self.raise_on_set = False
        self.before_set = None

    def set(self, active):
        if self.before_set:
            self.before_set(active)
        self.commands.append(active)
        if self.raise_on_set:
            raise RuntimeError("simulated relay exception")
        if active and self.fail_on:
            return {"ok": False, "active": False, "error": "ON failed"}
        if not active and self.fail_off_count > 0:
            self.fail_off_count -= 1
            return {"ok": False, "active": True, "error": "OFF failed"}
        self.active = active
        return {"ok": True, "active": active}

    def read(self):
        return {"ok": True, "active": self.active}


class GardenLightTimerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tempdir.name) / "garden-timer.sqlite3"
        self.clock = MutableClock()
        self.relay = FakeRelay()
        self.logs = []
        self.service = self.make_service()

    def tearDown(self):
        self.service.stop()
        self.tempdir.cleanup()

    def make_service(self, relay=None, interval=0.01):
        relay = relay or self.relay
        return GardenLightTimerService(
            self.db_path,
            "cesclans",
            "garden",
            "1",
            relay.set,
            relay.read,
            log_callback=self.logs.append,
            worker_interval_seconds=interval,
            clock=self.clock,
        )

    def rows(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM garden_light_timers ORDER BY generation")]

    def test_arming_is_durable_before_on_and_success_becomes_active(self):
        observed = []
        def inspect_before_set(active):
            observed.append(self.rows()[-1]["state"])
        self.relay.before_set = inspect_before_set
        result = self.service.start_timer(300)
        self.assertTrue(result["ok"])
        self.assertEqual(observed, ["ARMING"])
        self.assertEqual(self.rows()[-1]["state"], "ACTIVE")
        self.assertTrue(self.rows()[-1]["relay_on_confirmed"])
        self.assertTrue(result["deadline_at"].endswith("Z"))

    def test_failed_on_is_fault_not_false_active(self):
        self.relay.fail_on = True
        result = self.service.start_timer(300)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "FAULT")
        self.assertFalse(result["active"])
        self.assertFalse(self.rows()[-1]["relay_on_confirmed"])

    def test_unknown_on_outcome_preserves_off_obligation(self):
        self.relay.active = True
        self.relay.raise_on_set = True
        result = self.service.start_timer(300)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "RETRY_WAIT")
        self.assertTrue(result["active"])

    def test_normal_expiration_verifies_off_and_completes(self):
        self.service.start_timer(60)
        self.clock.advance(61)
        self.service.process_once()
        row = self.rows()[-1]
        self.assertEqual(row["state"], "COMPLETED")
        self.assertTrue(row["relay_off_confirmed"])
        self.assertEqual(self.relay.commands, [True, False])

    def test_failed_off_retries_until_confirmed(self):
        self.relay.fail_off_count = 1
        self.service.start_timer(10)
        self.clock.advance(11)
        self.service.process_once()
        row = self.rows()[-1]
        self.assertEqual(row["state"], "RETRY_WAIT")
        self.assertEqual(row["off_attempt_count"], 1)
        self.assertIn("OFF failed", row["last_error"])
        self.clock.advance(15)
        self.service.process_once()
        self.assertEqual(self.rows()[-1]["state"], "COMPLETED")
        self.assertEqual(self.rows()[-1]["off_attempt_count"], 2)

    def test_restart_before_deadline_resumes_without_off(self):
        self.service.start_timer(300)
        restarted = self.make_service()
        restarted.process_once()
        self.assertEqual(restarted.status()["state"], "ACTIVE")
        self.assertEqual(self.relay.commands, [True])

    def test_restart_after_deadline_immediately_turns_off(self):
        self.service.start_timer(30)
        self.clock.advance(31)
        restarted = self.make_service()
        restarted.process_once()
        self.assertEqual(restarted.status()["state"], "COMPLETED")
        self.assertFalse(self.relay.active)

    def test_legacy_deadline_is_migrated_once_without_sending_on(self):
        deadline = self.service._iso(self.clock() + timedelta(seconds=30))
        self.assertTrue(self.service.import_legacy_deadline(deadline))
        self.assertFalse(self.service.import_legacy_deadline(deadline))
        self.assertEqual(self.relay.commands, [])
        self.assertEqual(self.service.status()["state"], "ACTIVE")
        self.clock.advance(31)
        self.service.process_once()
        self.assertEqual(self.service.status()["state"], "COMPLETED")
        self.assertEqual(self.relay.commands, [False])

    def test_crash_after_on_before_active_is_recovered(self):
        now = self.service._iso(self.clock())
        with self.service._connect() as conn:
            conn.execute(
                """
                INSERT INTO garden_light_timers (
                    timer_id, generation, house_id, device_id, relay_number,
                    state, requested_duration_seconds, started_at, created_at, updated_at
                ) VALUES ('crash', 1, 'cesclans', 'garden', '1', 'ARMING', 60, ?, ?, ?)
                """,
                (now, now, now),
            )
        self.relay.active = True
        restarted = self.make_service()
        restarted.process_once()
        self.assertEqual(restarted.status()["state"], "ACTIVE")
        self.clock.advance(61)
        restarted.process_once()
        self.assertEqual(restarted.status()["state"], "COMPLETED")

    def test_second_timer_supersedes_first_and_stale_attempt_is_ignored(self):
        first = self.service.start_timer(30)
        second = self.service.start_timer(300)
        rows = self.rows()
        self.assertEqual(rows[0]["state"], "CANCELLED")
        self.assertEqual(rows[0]["cancellation_reason"], "SUPERSEDED")
        self.assertEqual(rows[1]["state"], "ACTIVE")
        self.service._attempt_off(first["timer_id"], first["generation"])
        self.assertEqual(self.service.status()["timer_id"], second["timer_id"])
        self.assertTrue(self.relay.active)

    def test_manual_off_success_cancels_but_failure_preserves_obligation(self):
        self.service.start_timer(300)
        self.relay.fail_off_count = 1
        failed = self.service.manual_off()
        self.assertFalse(failed["ok"])
        self.assertEqual(self.service.status()["state"], "RETRY_WAIT")
        self.clock.advance(15)
        self.service.process_once()
        self.assertEqual(self.service.status()["state"], "COMPLETED")

        self.service.start_timer(300)
        result = self.service.manual_off()
        self.assertTrue(result["ok"])
        self.assertEqual(self.rows()[-1]["state"], "CANCELLED")
        self.assertEqual(self.rows()[-1]["cancellation_reason"], "MANUAL_OFF")

    def test_manual_on_cancels_timer_and_is_not_auto_switched_off(self):
        self.service.start_timer(30)
        result = self.service.manual_on()
        self.assertTrue(result["ok"])
        self.assertFalse(result["active"])
        self.assertEqual(self.rows()[-1]["state"], "CANCELLED")
        self.assertEqual(self.rows()[-1]["cancellation_reason"], "MANUAL_ON")
        self.clock.advance(3600)
        self.service.process_once()
        self.assertTrue(self.relay.active)
        self.assertEqual(self.relay.commands[-1], True)

    def test_status_reads_do_not_mutate_or_control_relay(self):
        self.service.start_timer(300)
        before = self.rows()
        commands = list(self.relay.commands)
        for _ in range(5):
            self.service.status()
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.relay.commands, commands)

    def test_worker_survives_unexpected_exception_and_start_is_idempotent(self):
        original = self.service._watchdog
        calls = {"count": 0}
        def flaky():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("watchdog crash")
            return original()
        self.service._watchdog = flaky
        self.assertTrue(self.service.start())
        self.assertFalse(self.service.start())
        deadline = time.time() + 1
        while calls["count"] < 2 and time.time() < deadline:
            threading.Event().wait(0.01)
        self.assertGreaterEqual(calls["count"], 2)
        self.assertTrue(self.service.health()["running"])

    def test_watchdog_processes_expired_timed_on_but_not_manual_on(self):
        self.service.start_timer(1)
        self.clock.advance(2)
        self.service.process_once()
        self.assertFalse(self.relay.active)
        self.relay.active = True
        commands = list(self.relay.commands)
        self.clock.advance(600)
        self.service.process_once()
        self.assertEqual(self.relay.commands, commands)
        self.assertTrue(self.relay.active)

    def test_schema_initialization_is_repeatable_and_scope_is_fixed(self):
        other = self.make_service()
        other._init_db()
        self.service.start_timer(10)
        row = self.rows()[-1]
        self.assertEqual((row["house_id"], row["device_id"], row["relay_number"]), ("cesclans", "garden", "1"))


if __name__ == "__main__":
    unittest.main()
