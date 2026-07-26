import unittest
from unittest.mock import patch

import app as app_module


class FakeTimerService:
    def __init__(self):
        self.started = 0
        self.duration = None
        self.cancelled = None
        self.manual_actions = []

    def start(self):
        self.started += 1
        return self.started == 1

    def status(self):
        return {
            "timer_id": "timer-1", "generation": 1, "state": "ACTIVE",
            "active": True, "deadline_at": "2026-07-26T12:00:00Z",
            "remaining_seconds": 120, "off_attempt_count": 0,
            "next_retry_at": None, "last_error": None,
            "data_quality": "authoritative",
        }

    def start_timer(self, duration):
        self.duration = duration
        return {"ok": True, **self.status()}

    def cancel_current(self, reason):
        self.cancelled = reason
        return {"ok": True, "cancelled": True, "state": "CANCELLED", "active": False}

    def manual_on(self):
        self.manual_actions.append(True)
        return {"ok": True, "relay_status": {"ok": True, "active": True}}

    def manual_off(self):
        self.manual_actions.append(False)
        return {"ok": True, "relay_status": {"ok": True, "active": False}}


class GardenLightTimerApiTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeTimerService()
        self.patch = patch.object(app_module, "garden_light_timer_service", self.service)
        self.patch.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.patch.stop()

    def test_existing_timer_endpoint_starts_seconds_and_get_is_read_only(self):
        response = self.client.post("/api/garden/lights/timer", json={"minutes": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.duration, 600)
        status = self.client.get("/api/garden/lights/timer")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["state"], "ACTIVE")
        self.assertEqual(self.service.duration, 600)

    def test_invalid_duration_is_rejected(self):
        for value in (None, 0, 1441, "text"):
            response = self.client.post("/api/garden/lights/timer", json={"minutes": value})
            self.assertEqual(response.status_code, 400)

    def test_cancel_and_manual_commands_use_timer_service(self):
        cancelled = self.client.post("/api/garden/lights/timer/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(self.service.cancelled, "USER_CANCELLED")

        on = self.client.post("/api/relay", json={"device": "garden", "relay": "1", "active": True})
        off = self.client.post("/api/relay", json={"device": "garden", "relay": "1", "active": False})
        self.assertEqual((on.status_code, off.status_code), (200, 200))
        self.assertEqual(self.service.manual_actions, [True, False])

    def test_non_garden_relay_does_not_use_timer_service(self):
        with patch.object(
            app_module,
            "set_one",
            return_value={"ok": True, "active": True, "ignored": False},
        ) as relay_command:
            response = self.client.post(
                "/api/relay",
                json={"device": "pool", "relay": "1", "active": True},
            )
        self.assertEqual(response.status_code, 200)
        relay_command.assert_called_once()
        self.assertEqual(self.service.manual_actions, [])


if __name__ == "__main__":
    unittest.main()
