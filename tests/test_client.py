import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from client import AuthenticationRequired, Client, Credentials, SCOPES, Target


ENV = "00000000-0000-4000-8000-00000000c11e"
TOKEN = "synthetic-token-for-tests-only"


class Boundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.creds = Credentials(self.root)
        self.creds.save(ENV, {"access_token": TOKEN, "scope": " ".join(SCOPES), "expires_in": 3600})
        self.requests = []
        self.metadata = {"environmentId": ENV, "orchestrationProtocolVersion": 2}
        self.redirect = False
        self.pair_count = 0
        self.auth_status = 200
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, status, data, headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            def do_GET(self):
                parent.requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/.well-known/t3/environment":
                    if parent.redirect:
                        return self.respond(302, {}, {"Location": "/trap"})
                    return self.respond(200, parent.metadata)
                return self.respond(parent.auth_status, {"projects": [], "threads": []})
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
                parent.requests.append((self.path, self.headers.get("Authorization"), body))
                if self.path == "/oauth/token":
                    parent.pair_count += 1
                    return self.respond(403, {"error": "scope_not_granted"})
                return self.respond(200, {})
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.target = Target("fixture", "Fixture", f"http://127.0.0.1:{self.http.server_port}", ENV)

    async def test_environment_is_verified_before_credentials_are_sent(self):
        self.metadata["environmentId"] = "00000000-0000-4000-8000-000000000000"
        with self.assertRaisesRegex(ValueError, "different environment"):
            await Client(self.target, self.creds).shell()
        self.assertEqual(self.requests, [("/.well-known/t3/environment", None)])

    async def test_protocol_one_refuses_before_pairing_or_authenticated_calls(self):
        self.metadata["orchestrationProtocolVersion"] = 1
        with self.assertRaisesRegex(ValueError, "protocol 1"):
            await Client(self.target, self.creds).pair("test-bootstrap", "Test")
        self.assertEqual(self.pair_count, 0)
        self.assertTrue(all(request[1] is None for request in self.requests))

    async def test_redirect_is_not_followed_and_receives_no_credential(self):
        self.redirect = True
        with self.assertRaises(ValueError):
            await Client(self.target, self.creds).shell()
        self.assertEqual(self.requests, [("/.well-known/t3/environment", None)])

    async def test_pairing_refusal_does_not_retry_single_use_code(self):
        with self.assertRaises(AuthenticationRequired):
            await Client(self.target, self.creds).pair("test-bootstrap", "Test")
        self.assertEqual(self.pair_count, 1)
        self.assertIn("scope=orchestration%3Aoperate+orchestration%3Aread", self.requests[-1][2])

    async def test_credential_is_only_sent_to_the_confirmed_origin(self):
        await Client(self.target, self.creds).shell()
        self.assertEqual(self.requests[0], ("/.well-known/t3/environment", None))
        self.assertEqual(self.requests[1][:2], ("/api/orchestration/shell", "Bearer " + TOKEN))

    async def test_authentication_refusal_is_not_reclassified_as_network_failure(self):
        self.auth_status = 401
        with self.assertRaisesRegex(AuthenticationRequired, "rejected this client"):
            await Client(self.target, self.creds).shell()
        self.assertEqual(self.requests[0], ("/.well-known/t3/environment", None))
        self.assertEqual(self.requests[1][:2], ("/api/orchestration/shell", "Bearer " + TOKEN))

    async def test_provider_catalog_uses_real_v2_slugs_not_legacy_ids(self):
        client = Client(self.target, self.creds)
        async def catalog(method, payload):
            self.assertEqual((method, payload), ("server.getConfig", {}))
            return {"providers": [{"instanceId": "opencode", "driver": "opencode", "displayName": "OpenCode", "enabled": True, "installed": True,
                "models": [{"slug": "zai/glm-test", "name": "GLM test", "isDefault": True}, {"slug": "old", "name": "Old", "isLegacy": True}]}]}
        client.rpc = catalog
        self.assertEqual(await client.providers(), [{"instanceId": "opencode", "driver": "opencode", "displayName": "OpenCode", "models": ["zai/glm-test"], "ready": True}])

    def test_permissions_and_expiration_are_checked_at_the_boundary(self):
        with self.assertRaises(ValueError):
            self.creds.save(ENV, {"access_token": TOKEN, "scope": "orchestration:read orchestration:operate terminal:operate", "expires_in": 3600})
        file = self.creds.file(ENV)
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        data = json.loads(file.read_text())
        data["expires_at"] = 1
        file.write_text(json.dumps(data))
        with self.assertRaisesRegex(AuthenticationRequired, "expired"):
            self.creds.load(ENV)

    def test_nested_output_redacts_all_known_tokens(self):
        other = "00000000-0000-4000-8000-000000000000"
        self.creds.save(other, {"access_token": "other-synthetic-token", "scope": " ".join(SCOPES), "expires_in": 3600})
        value = {"error": "Bearer " + TOKEN, "nested": ["other-synthetic-token"]}
        self.assertEqual(self.creds.redact(value), {"error": "[redacted]", "nested": ["[redacted]"]})

    def test_origin_rejects_credentials_paths_and_cleartext_remote(self):
        for origin in ("https://user:secret@example.com", "http://remote.test", "https://example.com/path", "https://example.com?token=secret"):
            with self.assertRaises(ValueError):
                Target("remote", "Remote", origin, ENV, kind="remote")
