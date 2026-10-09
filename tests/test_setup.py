import json
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from client import AuthenticationRequired, Credentials
from domain import resolve_owner

ROOT = Path(__file__).resolve().parents[1]
ENV = "7d2a1c4e-3b5f-4a6d-9e8c-1f2a3b4c5d6e"


def setup_cli(config, *args, stdin=""):
    return subprocess.run([sys.executable, str(ROOT / "setup.py"), "--config-dir", str(config), *args],
                          input=stdin, capture_output=True, text=True, timeout=30)


class Setup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "v2"

    def test_init_creates_a_private_owner_once_and_resolve_owner_uses_it(self):
        first = setup_cli(self.config, "init")
        self.assertEqual(first.returncode, 0, first.stderr)
        created = json.loads(first.stdout)
        self.assertTrue(created["created"])
        uuid.UUID(created["owner"])
        file = self.config / "owner"
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)
        again = json.loads(setup_cli(self.config, "init").stdout)
        self.assertEqual((again["owner"], again["created"]), (created["owner"], False))
        self.assertEqual(resolve_owner({}, {}, {"T3_LAB_V2_CONFIG_DIR": str(self.config)}), created["owner"])

    def test_init_refuses_a_symlinked_config_directory_or_parent(self):
        real = self.config.parent / "elsewhere"
        real.mkdir()
        link = self.config.parent / "link"
        link.symlink_to(real)
        for config in (link, link / "v2"):
            result = setup_cli(config, "init")
            self.assertEqual(result.returncode, 2, config)
            self.assertIn("symlinked", result.stderr)
        self.assertEqual(list(real.iterdir()), [])

    def test_pair_fails_fast_without_a_terminal_or_token_stdin(self):
        result = setup_cli(self.config, "pair", "--target", "t", "--origin", "http://127.0.0.1:9", "--environment-id", ENV, "--accept-read-operate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--token-stdin", result.stderr)

    def test_pair_requires_an_environment_or_explicit_trust(self):
        result = setup_cli(self.config, "pair", "--target", "t", "--origin", "http://127.0.0.1:9", "--accept-read-operate", "--token-stdin", stdin="code")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--trust-inspected-environment", result.stderr)

    def test_projects_needs_a_paired_target(self):
        result = setup_cli(self.config, "projects", "--target", "missing")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Pair it first", result.stderr)

    def test_credentials_ignore_appledouble_and_reject_writable_directory(self):
        credentials = Credentials(self.config)
        credentials.root.mkdir(parents=True, mode=0o700)
        self.config.chmod(0o700)
        (credentials.root / "._junk.json").write_text("\x00\x05binary")
        credentials.save(ENV, {"scope": "orchestration:operate orchestration:read", "access_token": "synthetic-token", "expires_in": 60})
        self.assertEqual(credentials.load(ENV), "synthetic-token")
        self.assertEqual(credentials.redact("x synthetic-token"), "x [redacted]")
        credentials.root.chmod(0o777)
        with self.assertRaises(AuthenticationRequired):
            credentials.load(ENV)
        credentials.root.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
