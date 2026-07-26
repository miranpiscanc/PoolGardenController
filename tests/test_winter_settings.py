import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


class FakeWinterService:
    def __init__(self):
        self.updated = []
        self.sample_calls = 0
        self.target = 18.0

    def update_target_temperature(self, value):
        self.updated.append(value)
        self.target = value

    def sample_once(self):
        self.sample_calls += 1
        raise AssertionError("Settings must not create a Winter sample")

    def current(self):
        return {"decision": {"target_temperature": self.target}}


class WinterSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config_path = Path(self.tempdir.name) / "config.json"
        defaults = json.loads(app_module.CONFIG_DEFAULTS_PATH.read_text(encoding="utf-8"))
        defaults["houses"] = {
            "cesclans": {"winter": {"target_temperature": 18.0}},
            "opicina": {"winter": {"target_temperature": 16.0}},
        }
        defaults["winter"]["target_temp"] = 18.0
        self.config_path.write_text(json.dumps(defaults), encoding="utf-8")
        self.fake_service = FakeWinterService()
        self.path_patch = patch.object(app_module, "CONFIG_PATH", self.config_path)
        self.service_patch = patch.object(app_module, "winter_energy_service", self.fake_service)
        self.path_patch.start()
        self.service_patch.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.service_patch.stop()
        self.path_patch.stop()
        self.tempdir.cleanup()

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_get_reports_authoritative_cesclans_target(self):
        response = self.client.get("/api/winter/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["target_temperature"], 18.0)

    def test_valid_update_persists_without_sampling_and_keeps_houses_separate(self):
        response = self.client.post("/api/winter/settings", json={"target_temperature": 18.5})
        self.assertEqual(response.status_code, 200)
        saved = self.read_config()
        self.assertEqual(saved["houses"]["cesclans"]["winter"]["target_temperature"], 18.5)
        self.assertEqual(saved["houses"]["opicina"]["winter"]["target_temperature"], 16.0)
        self.assertEqual(saved["winter"]["target_temp"], 18.5)
        self.assertEqual(self.fake_service.updated, [18.5])
        self.assertEqual(self.fake_service.sample_calls, 0)
        self.assertEqual(
            self.client.get("/api/winter/current").get_json()["decision"]["target_temperature"],
            18.5,
        )
        with patch.object(app_module, "winter_energy_service", None):
            restarted = self.client.get("/api/winter/settings").get_json()
        self.assertEqual(restarted["target_temperature"], 18.5)

    def test_invalid_values_are_rejected_without_changing_config(self):
        before = copy.deepcopy(self.read_config())
        invalid = [11.5, 25.5, "testo", None, "", True, "NaN", "Infinity", 18.2]
        for value in invalid:
            response = self.client.post("/api/winter/settings", json={"target_temperature": value})
            self.assertEqual(response.status_code, 400, value)
        self.assertEqual(self.read_config(), before)
        self.assertEqual(self.fake_service.updated, [])


if __name__ == "__main__":
    unittest.main()
