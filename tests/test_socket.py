import asyncio
import json
import os
import ssl
import subprocess
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve

from client import Client, Credentials, MutationUnknown, Target
from test_client import ENV


class SocketBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def client(self, origin):
        client = Client(Target("test", "Test", origin, ENV), Credentials(self.root), timeout=2)
        async def ticket(*args, **kwargs):
            return {"ticket": "synthetic-one-use-ticket"}
        client.request = ticket
        return client

    async def test_websocket_does_not_follow_cross_origin_redirect(self):
        received = []
        async def trap(ws):
            received.append(await ws.recv())
            await ws.send(json.dumps({"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Success", "value": {"wrongOrigin": True}}}))
        async with serve(trap, "127.0.0.1", 0) as receiver:
            destination = f"ws://127.0.0.1:{receiver.sockets[0].getsockname()[1]}/trap"
            async def redirect(connection, request):
                response = connection.respond(HTTPStatus.TEMPORARY_REDIRECT, "Redirect refused by client")
                response.headers["Location"] = destination
                return response
            async with serve(trap, "127.0.0.1", 0, process_request=redirect) as original:
                client = self.client(f"http://127.0.0.1:{original.sockets[0].getsockname()[1]}")
                with self.assertRaises(MutationUnknown):
                    await client.rpc("orchestration.launchThread", {"privateTask": "synthetic"})
        self.assertEqual(received, [])

    async def test_tls_env_certificate_does_not_authorize_a_websocket(self):
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        # The refused handshake makes the server log a ConnectionResetError; it is the expected outcome.
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        def quiet(loop, context):
            if isinstance(context.get("exception"), ConnectionResetError):
                return
            if previous:
                previous(loop, context)
            else:
                loop.default_exception_handler(context)
        loop.set_exception_handler(quiet)
        self.addCleanup(loop.set_exception_handler, previous)
        received = []
        async def handler(ws):
            received.append(await ws.recv())
            await ws.send(json.dumps({"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Success", "value": {}}}))
        async with serve(handler, "127.0.0.1", 0, ssl=tls) as server:
            client = self.client(f"https://localhost:{server.sockets[0].getsockname()[1]}")
            with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert), "SSL_CERT_DIR": str(self.root)}):
                with self.assertRaises(MutationUnknown):
                    await client.rpc("orchestration.launchThread", {"privateTask": "synthetic"})
        self.assertEqual(received, [])
