import sqlite3
import tempfile
import unittest
import gc
from datetime import datetime
from pathlib import Path

from winter_energy_service import WinterEnergyService


def fresh_snapshot(indoor=19.0, pv=3000, load=1000, soc=70):
    timestamp = datetime.now().isoformat(timespec="seconds")
    metric = lambda value: {"temperature": {"value": value, "online": True}}
    return {
        "weather": {
            "online": True,
            "status": "online",
            "last_successful_poll": timestamp,
            "poll_seconds": 300,
            "stations": {},
            "modules": {
                "outside": {"id": "outside", "name": "Terasa", "home_name": "Cesclans", "type": "NAModule1", "metrics": metric(5)},
                "bed": {"id": "bed", "name": "Cezklanc (Spalnica)", "home_name": "Cesclans", "metrics": metric(indoor)},
                "kids": {"id": "kids", "name": "Soba Otroci", "home_name": "Cesclans", "metrics": metric(indoor)},
                "opicina": {"id": "opicina", "name": "Opicina bedroom", "home_name": "Opicina", "metrics": metric(-20)},
            },
        },
        "energy": {
            "pv_production": pv,
            "house_consumption": load,
            "battery_soc": soc,
        },
        "climate": {},
    }


class WinterAdvisorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tempdir.name) / "winter.sqlite"
        self.snapshot = fresh_snapshot()
        self.service = WinterEnergyService(
            self.db_path,
            {"sample_seconds": 300, "target_temp": 21.0},
            lambda: self.snapshot,
        )

    def tearDown(self):
        self.service.stop()
        del self.service
        gc.collect()
        self.tempdir.cleanup()

    def rows(self, table):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(f"SELECT * FROM {table}").fetchall()

    def test_repeatable_migration_preserves_existing_history(self):
        with sqlite3.connect(self.db_path) as conn:
            sample = conn.execute(
                "INSERT INTO winter_energy_history (timestamp) VALUES (?)",
                ("2026-01-01T00:00:00",),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO winter_hvac_history (
                    winter_sample_id, timestamp, collector_session_id, device_id,
                    data_quality, power, device_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sample, "2026-01-01T00:00:00", "legacy", "224571441890950", "online", 0, "off"),
            )
        self.service._init_db()
        self.service._init_db()
        self.assertEqual(len(self.rows("winter_energy_history")), 1)
        self.assertEqual(len(self.rows("winter_hvac_history")), 1)
        self.assertEqual(self.rows("winter_decision_history"), [])

    def test_one_decision_per_sample_and_current_refresh_is_read_only(self):
        current = self.service.sample_once()
        self.assertEqual(current["decision"]["decision_code"], "SIMULATE_PREHEAT")
        self.assertEqual(len(self.rows("winter_energy_history")), 1)
        self.assertEqual(len(self.rows("winter_decision_history")), 1)
        self.service.current()
        self.service.current()
        self.assertEqual(len(self.rows("winter_decision_history")), 1)
        decision = self.service.decision_history(limit=10)[0]
        self.assertEqual(decision["winter_sample_id"], 1)
        self.assertTrue(decision["simulation_only"])
        with self.service._connect() as conn:
            self.service._store_decision(conn, 1, decision["timestamp"], current["decision"])
        self.assertEqual(len(self.rows("winter_decision_history")), 1)

    def test_threshold_outcomes_and_reasons_are_authoritative(self):
        cases = [
            (fresh_snapshot(19, 3000, 1000, 70), "SIMULATE_PREHEAT", "PV_SURPLUS_AVAILABLE"),
            (fresh_snapshot(19, 2100, 1000, 70), "DEFER_HEATING", "PV_SURPLUS_INSUFFICIENT"),
            (fresh_snapshot(19, 3000, 1000, 49), "DEFER_HEATING", "BATTERY_SOC_LOW"),
            (fresh_snapshot(22, 3000, 1000, None), "NO_HEATING", "INDOOR_ABOVE_TARGET"),
            (fresh_snapshot(21, 3000, 1000, None), "HOLD", "DECISION_RULE_HOLD"),
        ]
        for snapshot, code, reason in cases:
            self.snapshot = snapshot
            decision = self.service.current()["decision"]
            self.assertEqual(decision["recommendation"], code)
            self.assertEqual(decision["decision_code"], code)
            self.assertEqual(decision["primary_reason_code"], reason)
            self.assertFalse(decision["hvac_command_sent"])

    def test_missing_and_stale_data_wait_honestly(self):
        self.snapshot = {"weather": {}, "energy": {}, "climate": {}}
        decision = self.service.current()["decision"]
        self.assertEqual(decision["decision_code"], "WAIT_FOR_DATA")
        self.assertEqual(decision["primary_reason_code"], "INPUT_DATA_STALE")
        self.assertIsNone(decision["inputs"]["battery_soc"])

    def test_hvac_unavailable_is_unknown_and_opicina_is_excluded(self):
        current = self.service.current()
        self.assertEqual(current["temperatures"]["indoor_average"], 19.0)
        self.assertTrue(all(device["power"] is None for device in current["hvac"]))
        self.assertTrue(all(device["state"] == "unknown" for device in current["hvac"]))

    def test_history_is_ordered_bounded_and_daily_counts_are_factual(self):
        for snapshot in (fresh_snapshot(19), fresh_snapshot(22), fresh_snapshot(21)):
            self.snapshot = snapshot
            self.service.sample_once()
        history = self.service.decision_history(limit=2)
        self.assertEqual(len(history), 2)
        self.assertGreaterEqual(history[0]["id"], history[1]["id"])
        summary = self.service.decision_daily_summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["counts"]["SIMULATE_PREHEAT"], 1)
        self.assertEqual(summary["counts"]["NO_HEATING"], 1)
        self.assertEqual(summary["counts"]["HOLD"], 1)

    def test_default_target_is_18_and_new_history_uses_it(self):
        service = WinterEnergyService(
            Path(self.tempdir.name) / "default-target.sqlite",
            {"sample_seconds": 300},
            lambda: fresh_snapshot(indoor=18.0),
        )
        current = service.sample_once()
        self.assertEqual(current["decision"]["target_temperature"], 18.0)
        self.assertEqual(current["decision"]["decision_code"], "HOLD")
        self.assertEqual(service.decision_history(limit=1)[0]["target_temperature"], 18.0)
        service.update_target_temperature(18.5)
        self.assertEqual(service.current()["decision"]["target_temperature"], 18.5)

    def test_target_update_does_not_rewrite_existing_decisions(self):
        self.service.sample_once()
        self.service.update_target_temperature(18.0)
        self.service.sample_once()
        targets = [row["target_temperature"] for row in self.service.decision_history(limit=10)]
        self.assertEqual(targets, [18.0, 21.0])


if __name__ == "__main__":
    unittest.main()
