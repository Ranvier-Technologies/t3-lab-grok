import json
import os
import ssl
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import asyncio
import certifi
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


SCOPES = {"orchestration:read", "orchestration:operate"}


class AuthenticationRequired(ValueError):
    pass


class MutationUnknown(ValueError):
    pass


class PinnedSocket(connect):
    def process_redirect(self, exc):
        return exc


def origin_url(value):
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or not parsed.hostname:
        raise ValueError("Use an exact server origin without credentials, path, query or fragment.")
    if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Non-loopback servers require HTTPS.")
    parsed.port
    return value.rstrip("/")


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    origin: str
    environment_id: str
    kind: str = "local"
    project_id: str | None = None  # legacy 0.3.x binding; kept for reference, no longer a spawn default
    project_root: str | None = None
    web_origin: str | None = None
    runtime_mode: str = "approval-required"
    default_project_id: str | None = None  # set only by `setup.py bind --project-id`

    def __post_init__(self):
        origin_url(self.origin)
        uuid.UUID(self.environment_id)
        if self.kind not in {"local", "remote"}:
            raise ValueError("Target kind must be local or remote.")
        if self.kind == "local" and urlsplit(self.origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("A local target must be loopback; use a remote target for other hosts.")
        if self.web_origin:
            origin_url(self.web_origin)
        if self.runtime_mode not in {"approval-required", "auto-accept-edits", "auto", "full-access"}:
            raise ValueError("Invalid configured runtime mode.")


def private_write(file, data):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=file.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, file)
        directory = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Credentials:
    def __init__(self, root):
        self.root = Path(root) / "credentials"

    def file(self, environment):
        return self.root / (str(uuid.UUID(environment)) + ".json")

    def save(self, environment, response):
        if set(response.get("scope", "").split()) != SCOPES or not response.get("access_token"):
            raise ValueError("Pairing returned unexpected permissions. Nothing was stored; revoke that client in T3 and pair again.")
        ttl = response.get("expires_in")
        if not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("Pairing did not return a valid expiration; nothing was stored.")
        private_write(self.file(environment), {"token": response["access_token"], "expires_at": time.time() + ttl, "scopes": sorted(SCOPES)})

    def load(self, environment):
        file = self.file(environment)
        if not file.exists():
            raise AuthenticationRequired("Pair this environment with the local setup CLI. Never paste pairing codes or tokens into the chat.")
        if file.is_symlink() or file.stat().st_mode & 0o077:
            raise AuthenticationRequired("Credential file must be private, mode 600, and not a symlink.")
        for directory in (self.root, self.root.parent):
            if directory.is_symlink() or directory.stat().st_mode & 0o022:
                raise AuthenticationRequired(f"Credential directory {directory.name or directory} must not be a symlink or writable by group/others (chmod 700).")
        data = json.loads(file.read_text())
        if set(data.get("scopes", [])) != SCOPES or not isinstance(data.get("token"), str) or not data["token"]:
            raise AuthenticationRequired("Stored permissions are invalid. Pair again; recorded tasks remain intact.")
        if data.get("expires_at", 0) <= time.time():
            raise AuthenticationRequired("This T3 client credential expired. Pair again; recorded tasks remain intact.")
        return data["token"]

    def redact(self, value):
        secrets = []
        for file in self.root.glob("*.json"):
            if file.name.startswith("._"):
                continue
            if not file.is_symlink() and not file.stat().st_mode & 0o077:
                try:
                    token = json.loads(file.read_text()).get("token")
                    if isinstance(token, str) and token:
                        secrets.append(token)
                except (ValueError, OSError):
                    pass
        def clean(item):
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace("Bearer " + secret, "[redacted]").replace(secret, "[redacted]")
                return item
            if isinstance(item, dict):
                return {key: clean(value) for key, value in item.items()}
            if isinstance(item, list):
                return [clean(value) for value in item]
            return item
        return clean(value)


class Client:
    def __init__(self, target, credentials, timeout=15):
        self.target, self.credentials, self.timeout = target, credentials, timeout
        self.metadata = None

    async def public_metadata(self):
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.get(self.target.origin + "/.well-known/t3/environment")
        except httpx.HTTPError:
            raise ValueError("T3 transport could not read public metadata from the configured origin. No credentials were sent.") from None
        if response.status_code != 200:
            raise ValueError("The configured T3 origin did not return public metadata; no credentials were sent.")
        data = response.json()
        if data.get("environmentId") != self.target.environment_id:
            raise ValueError("The origin now identifies a different environment; no credentials were sent.")
        if data.get("orchestrationProtocolVersion") != 2:
            raise ValueError("This target still uses orchestration protocol 1 or an unsupported version. Upgrade it separately; no pairing or operation was sent.")
        self.metadata = data
        return data

    async def request(self, method, path, *, data=None, authenticated=True):
        if self.metadata is None:
            await self.public_metadata()
        headers = {"x-t3-orchestration-protocol": "2"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.credentials.load(self.target.environment_id)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.request(method, self.target.origin + path, headers=headers, data=data)
        except httpx.HTTPError:
            raise ValueError("T3 transport could not read the configured origin. No destination fallback was attempted.") from None
        if response.status_code in {401, 403}:
            raise AuthenticationRequired("T3 rejected this client. Pair again or check its permissions; do not try another environment.")
        if 300 <= response.status_code < 400:
            raise ValueError("T3 redirected a request. Redirects are not followed and credentials were not forwarded.")
        if response.status_code != 200:
            raise ValueError(f"T3 returned HTTP {response.status_code} from the configured origin.")
        return response.json()

    async def pair(self, bootstrap, label):
        await self.public_metadata()
        response = await self.request("POST", "/oauth/token", authenticated=False, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": bootstrap,
            "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": " ".join(sorted(SCOPES)), "client_label": label})
        self.credentials.save(self.target.environment_id, response)
        return {"paired": True, "environmentId": self.target.environment_id, "scopes": sorted(SCOPES), "expiresIn": response["expires_in"]}

    async def shell(self):
        return await self.request("GET", "/api/orchestration/shell")

    async def projection(self, thread_id):
        thread_id = str(uuid.UUID(thread_id))
        response = await self.request("GET", "/api/orchestration/threads/" + thread_id)
        projection = response.get("projection")
        if not isinstance(projection, dict) or projection.get("thread", {}).get("id") != thread_id:
            raise ValueError("T3 did not return the requested thread projection.")
        return projection

    async def rpc(self, method, payload):
        if method not in {"server.getConfig", "orchestration.launchThread", "orchestration.dispatchCommand", "projects.mutate", "projects.ensureScratch"}:
            raise ValueError("This facade does not expose that RPC method.")
        ticket = await self.request("POST", "/api/auth/websocket-ticket")
        uri = self.target.origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        uri += "/ws?" + urlencode({"orchestrationProtocol": "2", "wsTicket": ticket["ticket"]})
        try:
            async with asyncio.timeout(self.timeout):
                tls = {"ssl": ssl.create_default_context(cafile=certifi.where())} if uri.startswith("wss://") else {}
                async with PinnedSocket(uri, proxy=None, open_timeout=self.timeout, max_size=8 * 1024 * 1024, **tls) as ws:
                    await ws.send(json.dumps({"_tag": "Request", "id": "1", "tag": method, "payload": payload, "headers": []}))
                    async for message in ws:
                        frame = json.loads(message)
                        if frame.get("_tag") == "Ping":
                            await ws.send(json.dumps({"_tag": "Pong"}))
                        if frame.get("_tag") == "Exit" and frame.get("requestId") == "1":
                            exit = frame["exit"]
                            if exit.get("_tag") == "Success":
                                return exit.get("value")
                            raise MutationUnknown("T3 returned an RPC failure. Reconcile this exact operation; no new identifier was generated.")
                        if frame.get("_tag") in {"Defect", "ClientProtocolError"}:
                            raise MutationUnknown("T3 could not complete this RPC. Reconcile the saved operation with its original identifiers.")
            raise MutationUnknown("T3 closed the connection without confirmation. Recover the same recorded operation.")
        except (TimeoutError, OSError, ConnectionError, WebSocketException):
            raise MutationUnknown("T3 transport did not confirm the operation. Recover the same requestKey; never launch a replacement.") from None

    async def providers(self):
        config = await self.rpc("server.getConfig", {})
        return [{"instanceId": provider["instanceId"], "driver": provider["driver"],
                 "displayName": provider.get("displayName") or provider["driver"],
                 "models": [model["slug"] for model in provider.get("models", []) if not model.get("isLegacy")],
                 "ready": provider.get("enabled", False) and provider.get("installed", False) and provider.get("availability", "available") == "available"}
                for provider in config.get("providers", [])]


def load_targets(root):
    file = Path(root) / "targets.json"
    if not file.exists():
        return []
    data = json.loads(file.read_text())
    if data.get("version") != 2 or not isinstance(data.get("targets"), list):
        raise ValueError("Invalid V2 target configuration. Legacy projects.json is not imported.")
    targets = [Target(**item) for item in data["targets"]]
    if len({target.id for target in targets}) != len(targets):
        raise ValueError("Configured target IDs must be unique.")
    return targets


def save_target(root, target):
    targets = load_targets(root)
    current = next((found for found in targets if found.id == target.id), None)
    if current is not None and current.environment_id != target.environment_id:
        raise ValueError("This target ID belongs to a different environment. Choose a new ID; no configuration was overwritten.")
    targets = [found for found in targets if found.id != target.id] + [target]
    private_write(Path(root) / "targets.json", {"version": 2, "targets": [asdict(found) for found in targets]})
