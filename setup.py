import argparse
import asyncio
import getpass
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from client import Client, Credentials, Target, load_targets, origin_url, save_target


def parser():
    root = argparse.ArgumentParser(description="Configure the T3 client facade. Pairing secrets never belong in chat or tool arguments.")
    root.add_argument("--config-dir", type=Path, default=Path(os.environ.get("T3_LAB_V2_CONFIG_DIR", Path.home() / ".config/t3-lab/v2")))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the private owner UUID file (mode 600) if missing and print it. Safe to re-run.")
    pair = commands.add_parser("pair", help="Exchange one operator-provided pairing code once, requesting only read+operate.")
    pair.add_argument("--target", required=True)
    pair.add_argument("--origin", required=True)
    pair.add_argument("--environment-id", help="Expected environment ID from T3's public metadata (see inspect). Omit only with --trust-inspected-environment.")
    pair.add_argument("--trust-inspected-environment", action="store_true", help="Use the environment ID the origin reports right now instead of passing --environment-id.")
    pair.add_argument("--kind", choices=["local", "remote"], default="local")
    pair.add_argument("--label", default="Cursor plugin")
    pair.add_argument("--web-origin", help="Optional user-openable web origin. Never inherited from another environment.")
    pair.add_argument("--accept-read-operate", action="store_true", help="Explicit consent to these broad orchestration scopes; no terminal/review/relay scopes are requested.")
    pair.add_argument("--token-stdin", action="store_true", help="For a trusted local helper: read the code from stdin, not argv. Interactive use prompts with echo disabled.")
    bind = commands.add_parser("bind", help="Optional: set a target's default project (used when spawn_thread names none) and/or its default runtime mode.")
    bind.add_argument("--target", required=True)
    bind.add_argument("--project-id", help="Default project id from `projects`.")
    bind.add_argument("--project-root", help="Ignored if given; kept for 0.3.x scripts. The root is read from T3.")
    bind.add_argument("--runtime-mode", "--runtime-ceiling", dest="runtime_mode", choices=["approval-required", "auto-accept-edits", "auto", "full-access"],
                      help="Default runtime mode; spawn_thread may override it per call.")
    inspect = commands.add_parser("inspect", help="Read public metadata without credentials.")
    inspect.add_argument("--origin", required=True)
    projects = commands.add_parser("projects", help="List every project (id, title, workspaceRoot) in a paired target's environment, marking the default.")
    projects.add_argument("--target", required=True)
    status = commands.add_parser("status", help="Show configuration and credential expiry without revealing tokens.")
    status.add_argument("--probe", action="store_true", help="Read live provider discovery; configured alone is not availability.")
    return root


async def run(args):
    import httpx
    credentials = Credentials(args.config_dir)
    if args.command == "init":
        directory = Path(os.path.abspath(args.config_dir))
        for part in [directory, *directory.parents]:
            # A symlink anywhere on the path could redirect the owner file. Root-owned system links (macOS /var, /tmp) are fine.
            if part.is_symlink() and part.lstat().st_uid != 0:
                raise ValueError(f"Refusing a symlinked config path component: {part}. Use a real directory; nothing was written.")
        file = directory / "owner"
        if file.exists() and not file.is_symlink():
            owner = str(uuid.UUID(file.read_text().strip()))
            if file.stat().st_mode & 0o077:
                file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            return {"owner": owner, "created": False, "file": str(file)}
        owner = str(uuid.uuid4())
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(owner + "\n")
        return {"owner": owner, "created": True, "file": str(file)}
    if args.command == "projects":
        target = next((target for target in load_targets(args.config_dir) if target.id == args.target), None)
        if target is None:
            raise ValueError("Unknown target. Pair it first, then list its projects.")
        projects = (await Client(target, credentials).shell()).get("projects", [])
        return {"target": target.id, "projects": [{"id": project.get("id"), "title": project.get("title"), "workspaceRoot": project.get("workspaceRoot"),
                                                    "default": project.get("id") == target.default_project_id} for project in projects]}
    if args.command == "inspect":
        origin = origin_url(args.origin)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10) as session:
            response = await session.get(origin + "/.well-known/t3/environment")
        if response.status_code != 200:
            raise ValueError("The selected origin did not return public metadata; no credentials were sent.")
        data = response.json()
        return {key: data.get(key) for key in ("environmentId", "label", "serverVersion", "orchestrationProtocolVersion")}
    if args.command == "pair":
        if not args.accept_read_operate:
            raise ValueError("Review the scopes and pass --accept-read-operate. Pairing can be consumed even on failure, so validate before entering the code.")
        if not args.token_stdin and not sys.stdin.isatty():
            raise ValueError("No terminal for a hidden prompt. Pipe the code with --token-stdin (for example from an environment variable); no code was read.")
        if not args.environment_id:
            if not args.trust_inspected_environment:
                raise ValueError("Pass --environment-id from inspect, or --trust-inspected-environment to use what the origin reports now.")
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10) as session:
                response = await session.get(origin_url(args.origin) + "/.well-known/t3/environment")
            if response.status_code != 200:
                raise ValueError("The selected origin did not return public metadata; no code was read.")
            args.environment_id = response.json().get("environmentId")
        target = Target(args.target, args.label, origin_url(args.origin), args.environment_id, args.kind,
                        web_origin=origin_url(args.web_origin) if args.web_origin else None)
        known = next((target for target in load_targets(args.config_dir) if target.id == args.target), None)
        if known and known.environment_id != args.environment_id:
            raise ValueError("This target ID belongs to another environment. Choose a new ID before pairing; no code was consumed.")
        client = Client(target, credentials)
        await client.public_metadata()
        code = sys.stdin.read().strip() if args.token_stdin else getpass.getpass("Pairing code or link, hidden input: ").strip()
        if code.startswith(("http://", "https://")):
            link = urlsplit(code)
            if link.path != "/pair":
                raise ValueError("The pairing input is a link but not a /pair link; no code was used.")
            if f"{link.scheme}://{link.netloc}".rstrip("/") != target.origin:
                print("Warning: the pairing link names a different address than --origin; only its token is used, "
                      "sent to --origin.", file=sys.stderr)
            code = parse_qs(link.fragment).get("token", [""])[0]
        if not code or len(code) > 4096 or any(character.isspace() for character in code):
            raise ValueError("Pairing input is empty or invalid; its value was not printed or used.")
        result = await client.pair(code, args.label)
        if known:
            target = Target(**{**known.__dict__, "origin": target.origin, "label": target.label, "web_origin": target.web_origin})
        save_target(args.config_dir, target)
        return result
    if args.command == "bind":
        target = next((target for target in load_targets(args.config_dir) if target.id == args.target), None)
        if target is None:
            raise ValueError("Pair the target before setting its defaults.")
        changes = {}
        if args.project_id:
            projects = (await Client(target, credentials).shell()).get("projects", [])
            project = next((project for project in projects if project["id"] == args.project_id), None)
            if project is None:
                raise ValueError("That project id is not in this environment; run `projects`. Nothing was changed.")
            changes.update(default_project_id=project["id"])
        if args.runtime_mode:
            changes["runtime_mode"] = args.runtime_mode
        if not changes:
            raise ValueError("Pass --project-id and/or --runtime-mode. Nothing was changed.")
        target = Target(**{**target.__dict__, **changes})
        save_target(args.config_dir, target)
        return {"bound": True, "target": target.id, "defaultProjectId": target.default_project_id, "runtimeMode": target.runtime_mode}
    results = []
    for target in load_targets(args.config_dir):
        row = {"id": target.id, "label": target.label, "environmentId": target.environment_id, "origin": target.origin, "kind": target.kind}
        try:
            credentials.load(target.environment_id)
            row["authentication"] = "stored"
            if args.probe:
                row["providers"] = await Client(target, credentials).providers()
        except Exception:
            row["authentication"] = "needs_pairing_or_connection_check"
        results.append(row)
    return {"targets": results}


def main():
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
        print(json.dumps(Credentials(args.config_dir).redact(result), ensure_ascii=False))
    except Exception as error:
        message = Credentials(args.config_dir).redact(str(error))
        print(json.dumps({"error": message, "pairingRetried": False}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
