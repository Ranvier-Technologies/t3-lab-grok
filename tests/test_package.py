import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path


OWNER_FIXTURE = "00000000-0000-4000-8000-000000000001"
UUIDS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Every UUID a shipped file may contain: synthetic test fixtures only.
FIXTURE_UUIDS = {
    "00000000-0000-4000-8000-000000000000", OWNER_FIXTURE, "00000000-0000-4000-8000-00000000c11e",
    "019f0aae-1111-4222-8333-444455556666", "019f0aae-1111-4222-8333-777788889999",
    "019f0aae-aaaa-4222-8333-444455556666", "7d2a1c4e-3b5f-4a6d-9e8c-1f2a3b4c5d6e",
}
PERSONAL = re.compile(r"/Users/[a-z]|/home/(?!box\b)[a-z]|\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d+\.\d+|[a-z0-9-]+\.tail[0-9a-f]{4,}\.ts\.net|tskey-|\bsk-[A-Za-z0-9]{16}", re.IGNORECASE)
SPANISH = re.compile(r"[áéíóúñ¿¡]|\b(abrir|copiar|hilo|tarea|encargo|aviso|seguimiento|cierre)\b", re.IGNORECASE)


def tracked_text_files(root):
    names = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True).stdout.split()
    if not names:
        names = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and not {".git", ".venv", "__pycache__"} & set(path.parts)]
    for name in names:
        path = root / name
        if path.is_file() and path.suffix not in {".png", ".lock"}:
            yield name, path.read_text(encoding="utf-8", errors="replace")


class Package(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]

    def test_identity_prompt_and_official_logo_are_preserved(self):
        self.assertFalse((self.root / "plugin.json").exists(), "root Agent plugin.json must not sit beside the Cursor Plugin manifest")
        manifest = json.loads((self.root / ".cursor-plugin" / "plugin.json").read_text())
        interface = manifest["extensions"]["com.openai"]["interface"]
        self.assertEqual((manifest["name"], manifest["version"], interface["displayName"]), ("t3-lab-grok", "0.4.3", "T3 Lab Grok"))
        self.assertEqual(interface["defaultPrompt"], "Ask Claude and Grok for a second opinion on a file, in parallel.")
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        self.assertNotIn("Local private use", interface["longDescription"])
        for key in ("logo", "composerIcon"):
            self.assertEqual(interface[key], "./assets/t3-code.png")
        self.assertEqual(manifest["logo"], "assets/t3-code.png")
        owner = manifest["variables"]["properties"]["T3_LAB_OWNER"]
        self.assertNotIn("required", manifest["variables"], "T3_LAB_OWNER is optional; setup.py init writes an owner file")
        self.assertIn("Generate your own", owner["description"])
        self.assertTrue((self.root / "LICENSE").is_file())
        self.assertNotIn("mcpServers", manifest)
        logo = (self.root / "assets/t3-code.png").read_bytes()
        self.assertEqual(hashlib.sha256(logo).hexdigest(), "4943ac41cc004f826c95d6da34e0b04c387cf54982fc424ba3da8b68dc4598db")
        self.assertEqual(logo[:8], bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]))
        self.assertEqual((int.from_bytes(logo[16:20], "big"), int.from_bytes(logo[20:24], "big")), (180, 180))
        self.assertIn("T3 Tools", (self.root / "assets/T3-LICENSE.txt").read_text())
        self.assertIn('version = "0.4.3"', (self.root / "pyproject.toml").read_text())

    def test_runtime_uses_the_packaged_entrypoint_not_legacy_board(self):
        runtime = json.loads((self.root / "mcp.json").read_text())["mcpServers"]
        self.assertEqual(set(runtime), {"t3-lab-grok"})
        server = runtime["t3-lab-grok"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "uv")
        self.assertEqual(server["args"], ["run", "--frozen", "python", "server.py"])
        self.assertEqual(server["cwd"], "${CURSOR_PLUGIN_ROOT}")
        self.assertNotIn("${PLUGIN_ROOT}", json.dumps(runtime))
        self.assertEqual(server["env"]["T3_LAB_OWNER"], "${T3_LAB_OWNER}")
        self.assertNotIn("UV_PROJECT_ENVIRONMENT", server["env"])
        self.assertFalse((self.root / "board.html").exists())
        self.assertTrue((self.root / "uv.lock").is_file())

    def test_skill_has_matching_frontmatter_and_no_legacy_recovery(self):
        skill = (self.root / "skills/t3-delegate/SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: t3-delegate\ndescription:"))
        self.assertLess(len(skill.splitlines()), 500)
        self.assertIn("never invent a replacement thread", skill)
        self.assertIn("outside the chat", skill)
        self.assertIn("setup.py", skill)
        self.assertNotIn("delegation_board", skill)
        self.assertIn("T3_LAB_OWNER", skill)
        self.assertIn("Generate your own owner UUID", skill)
        self.assertIn("show_delegation` once", skill)
        self.assertIn("phase` `handoff`", skill)
        self.assertIn("phase` `wrap-up` only if no card rendered", skill)
        self.assertIn("Always paste the tool `content`", skill)
        self.assertIn("tool-result panel", skill)
        self.assertNotIn("do not paste a second digest", skill)
        self.assertNotIn("](t3code://", skill)
        self.assertEqual(OWNER_FIXTURE, "00000000-0000-4000-8000-000000000001")

    def test_shipped_files_carry_only_fixture_uuids_and_no_personal_hosts(self):
        for name, text in tracked_text_files(self.root):
            if name == "tests/test_package.py":
                continue
            for found in UUIDS.findall(text):
                self.assertIn(found.lower(), FIXTURE_UUIDS, f"{name} carries a non-fixture UUID")
            self.assertIsNone(PERSONAL.search(text), f"{name} carries a personal path, host, IP or token")

    def test_english_only_outside_deprecated_alias_handling(self):
        allowed = {"domain.py", "server.py", "ui/delegation-card.html", "README.md", "CHANGELOG.md", "tests/test_apps.py", "tests/test_package.py"}
        for name, text in tracked_text_files(self.root):
            for number, line in enumerate(text.splitlines(), 1):
                if SPANISH.search(line) and not (name in allowed and re.search(r"alias|deprecated|MOMENT_ALIASES|PHASE_ALIAS|SPANISH|SENTENCE_END|encargo.*handoff|0\.3\.[0-6]", line, re.IGNORECASE)):
                    self.fail(f"{name}:{number} is not English: {line.strip()[:80]}")

    def test_setup_skill_is_shipped_and_referenced(self):
        manifest = json.loads((self.root / ".cursor-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["skills"], "./skills/")
        for key in ("repository", "homepage", "license"):
            self.assertTrue(manifest[key])
        skill = (self.root / "skills/t3-lab-setup/SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: t3-lab-setup\ndescription:"))
        for needle in ("T3_PAIRING_CODE", "TS_AUTHKEY", "--token-stdin", "setup.py init", "setup.py projects", "doctor-targets.py", "/.well-known/t3/environment", "never"):
            self.assertIn(needle, skill)
        self.assertIsNone(PERSONAL.search(skill))
        self.assertIsNone(SPANISH.search(skill))
        self.assertFalse(UUIDS.findall(skill))
        self.assertIn("skills/t3-lab-setup", (self.root / "README.md").read_text())

