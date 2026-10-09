import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

from engine import place_label
from server import describe_validation_error

ROOT = Path(__file__).resolve().parents[1]


class OnboardingFixes(unittest.TestCase):
    def error(self, schema, instance):
        with self.assertRaises(jsonschema.ValidationError) as caught:
            jsonschema.validate(instance, schema)
        return describe_validation_error(caught.exception)

    def test_missing_field_is_named(self):
        schema = {"type": "object", "properties": {"a": {}, "b": {}}, "required": ["a", "b"]}
        self.assertEqual(self.error(schema, {"a": "secret-value"}), "missing required field b in (arguments)")

    def test_extra_field_is_named_without_its_value(self):
        schema = {"type": "object", "properties": {"m": {"type": "object", "properties": {"x": {}}, "additionalProperties": False}}}
        message = self.error(schema, {"m": {"x": 1, "provider": "secret-value"}})
        self.assertIn("unexpected field provider in m", message)
        self.assertNotIn("secret-value", message)

    def test_place_label_distinguishes_scratch_folders(self):
        self.assertEqual(place_label("/r", "/r", True), "main checkout")
        self.assertEqual(place_label("/u/.t3/scratch/2026-x", "/u/.t3/scratch", False), "scratch folder scratch/2026-x")
        self.assertEqual(place_label("/w/repo-wt/feat", "/u/repo", False), "worktree repo-wt/feat")
        self.assertEqual(place_label("/home/box/.t3/scratch/2026-y", "/home/box/.t3/scratch/", False), "scratch folder scratch/2026-y")

    def test_subfolder_of_a_regular_project_keeps_worktree_label(self):
        self.assertEqual(place_label("/u/repo/.worktrees/feat", "/u/repo", False), "worktree .worktrees/feat")
        self.assertEqual(place_label("/u/scratch/a", "/u/scratch", False), "worktree scratch/a")

    def test_doctor_honors_config_dir_env(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = tmp.name
        env = {**os.environ, "T3_LAB_V2_CONFIG_DIR": directory}
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "doctor-targets.py"), "--json"],
                                capture_output=True, text=True, env=env)
        self.assertIn(str(Path(directory) / "targets.json"), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
