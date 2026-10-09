import copy
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Journal:
    def __init__(self, root: Path, owner: str):
        self.owner = str(uuid.UUID(owner))
        directory = Path(root) / hashlib.sha256(self.owner.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.file = directory / "journal.sqlite"
        with self.database() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, target TEXT NOT NULL, environment TEXT NOT NULL,
                thread_id TEXT NOT NULL, command_id TEXT NOT NULL, message_id TEXT NOT NULL,
                signature TEXT NOT NULL, request TEXT, intent TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, receipt TEXT, created_at REAL NOT NULL)""")
        self.file.chmod(0o600)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.file, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError("No operation belongs to this chat. Do not adopt a thread from another chat.")
        data = dict(row)
        for key in ("request", "intent", "payload", "receipt"):
            data[key] = json.loads(data[key]) if data[key] else None
        return data

    def prepare(self, key, kind, target, environment, intent, thread_id=None, request=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key):
            raise ValueError("requestKey must be a stable 1-128 character key using letters, digits, dots, colons, underscores or hyphens.")
        if kind not in {"launch", "send"}:
            raise ValueError("Unsupported operation.")
        if kind == "send":
            self.latest(thread_id)
        encoded = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        requested = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False) if request is not None else None
        signature = hashlib.sha256(json.dumps([kind, target, environment, thread_id, requested or encoded]).encode()).hexdigest()
        with self.database() as db:
            row = db.execute("SELECT * FROM operations WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["signature"] != signature:
                    raise ValueError("This requestKey already records a different operation. Recover the original or use a new key for a new task; nothing was sent.")
                return self.decode(row)
            command = str(uuid.uuid4())
            message = str(uuid.uuid4())
            thread = thread_id or str(uuid.uuid4())
            payload = copy.deepcopy(intent)
            payload.update(commandId=command, threadId=thread)
            if kind == "launch":
                payload["initialMessage"]["messageId"] = message
            else:
                payload.update(type="message.dispatch", messageId=message)
            db.execute("""INSERT INTO operations
                (request_key,kind,target,environment,thread_id,command_id,message_id,signature,request,intent,payload,state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (key, kind, target, environment, thread, command, message,
                    signature, requested, encoded, json.dumps(payload, ensure_ascii=False), "prepared", time.time()))
            return self.decode(db.execute("SELECT * FROM operations WHERE request_key=?", (key,)).fetchone())

    def mark(self, key, state, receipt=None):
        if state not in {"accepted", "unknown", "refused"}:
            raise ValueError("Unsupported operation state.")
        with self.database() as db:
            found = db.execute("UPDATE operations SET state=?, receipt=? WHERE request_key=?",
                               (state, json.dumps(receipt) if receipt is not None else None, key))
            if found.rowcount != 1:
                raise ValueError("No recorded operation to confirm.")

    def latest(self, thread_id):
        with self.database() as db:
            row = db.execute("SELECT * FROM operations WHERE thread_id=? AND state!='refused' ORDER BY seq DESC LIMIT 1", (thread_id,)).fetchone()
            data = self.decode(row)
            data["round"] = db.execute("SELECT COUNT(*) FROM operations WHERE thread_id=? AND state!='refused' AND seq<=?", (thread_id, data["seq"])).fetchone()[0]
            return data

    def threads(self):
        with self.database() as db:
            rows = db.execute("SELECT thread_id, MIN(seq) AS first_seq FROM operations WHERE kind='launch' GROUP BY thread_id ORDER BY first_seq DESC LIMIT 100").fetchall()
        return [self.latest(row["thread_id"]) for row in reversed(rows)]

    def get(self, key):
        with self.database() as db:
            return self.decode(db.execute("SELECT * FROM operations WHERE request_key=?", (key,)).fetchone())

    def find(self, key):
        with self.database() as db:
            row = db.execute("SELECT * FROM operations WHERE request_key=?", (key,)).fetchone()
            return self.decode(row) if row else None

    def launch(self, thread_id):
        with self.database() as db:
            return self.decode(db.execute("SELECT * FROM operations WHERE thread_id=? AND kind='launch' ORDER BY seq LIMIT 1", (thread_id,)).fetchone())
