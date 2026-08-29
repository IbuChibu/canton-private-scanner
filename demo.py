"""Safe one-command launcher for the local Canton scanner demo."""

import argparse
import importlib.util
import os
import socket
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - the demo target is macOS/Linux.
    fcntl = None


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"
REQUIRED_MODULES = ("fastapi", "uvicorn", "websocket")


class DemoError(RuntimeError):
    """A startup problem that can be shown without a traceback."""


def load_env_file(path=DEFAULT_ENV_FILE, environ=None):
    """Load simple KEY=VALUE entries without printing or overriding the shell."""

    environ = os.environ if environ is None else environ
    path = Path(path)
    if not path.exists():
        return False
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise DemoError(f"Invalid .env entry on line {line_number}.")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise DemoError(f"Invalid environment name on line {line_number}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        environ.setdefault(key, value)
    return True


def dependency_errors():
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if not missing:
        return []
    return [
        "Missing Python packages: " + ", ".join(missing) + ".",
        "Activate .venv and run: python -m pip install -r requirements.txt",
    ]


def _validate_url(name, value, schemes):
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.netloc:
        expected = " or ".join(sorted(schemes))
        raise DemoError(f"{name} must be a complete {expected} URL.")


def validate_environment(environ=None):
    environ = os.environ if environ is None else environ
    base_url = environ.get("C8_BASE", "http://localhost:2975")
    websocket_url = environ.get(
        "C8_WS_URL",
        "wss://api.validator.dev.digik.cantor8.tech/api/ledger/v2/updates",
    )
    _validate_url("C8_BASE", base_url, {"http", "https"})
    _validate_url("C8_WS_URL", websocket_url, {"ws", "wss"})

    identity_provider = environ.get("C8_IDP")
    if identity_provider:
        _validate_url("C8_IDP", identity_provider, {"http", "https"})
        secret = environ.get("C8_CLIENT_SECRET", "")
        if not secret or secret.lower() in {"replace-me", "change-me", "placeholder"}:
            raise DemoError(
                "C8_IDP is configured but C8_CLIENT_SECRET is missing. "
                "Add the DevNet client secret to the ignored .env file."
            )

    database_path = Path(environ.get("SCANNER_DB", "scanner.db")).expanduser()
    if not database_path.is_absolute():
        database_path = PROJECT_DIR / database_path
    if not database_path.parent.exists():
        raise DemoError(f"Database directory does not exist: {database_path.parent}")
    if database_path.exists() and not database_path.is_file():
        raise DemoError(f"SCANNER_DB is not a file: {database_path}")
    return database_path


def check_database(database_path):
    """Verify SQLite integrity and write locking without deleting or resetting data."""

    try:
        connection = sqlite3.connect(database_path, timeout=2)
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise DemoError("SQLite integrity check did not return ok.")
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        connection.close()
    except (OSError, sqlite3.Error) as error:
        raise DemoError(
            f"Scanner database is unavailable or not writable: {database_path} ({error})"
        ) from error


class DemoLock:
    """Prevent two managed demo launchers from supervising the same SQLite file."""

    def __init__(self, database_path):
        self.path = Path(f"{database_path}.demo.lock")
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+")
        if fcntl is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                self.handle.close()
                self.handle = None
                raise DemoError(
                    "Another managed demo is already using this scanner database."
                ) from error
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, _error_type, _error, _traceback):
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def check_port(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as error:
        raise DemoError(f"Cannot bind the local dashboard at {host}:{port}: {error}") from error


def check_participant():
    """Make one read-only Ledger API request and return the current offset."""

    import c8lab

    try:
        return int(c8lab.ledger_end())
    except Exception as error:
        raise DemoError(f"Canton participant preflight failed: {error}") from error


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Launch the local Canton scanner demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate configuration, database, dependencies, and participant, then exit",
    )
    parser.add_argument(
        "--skip-network-check",
        action="store_true",
        help="start immediately and let the managed worker report participant availability",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        loaded_env = load_env_file(args.env_file)
        errors = dependency_errors()
        if errors:
            raise DemoError(" ".join(errors))
        os.environ["SCANNER_RUN_WORKER"] = "1"
        database_path = validate_environment()
        os.environ["SCANNER_DB"] = str(database_path)

        with DemoLock(database_path):
            check_database(database_path)
            participant_offset = None
            participant_error = None
            if not args.skip_network_check:
                try:
                    participant_offset = check_participant()
                except DemoError as error:
                    participant_error = error
                    if args.check_only:
                        raise

            print("Local demo preflight")
            print("  environment:", "loaded .env" if loaded_env else "shell variables")
            print("  database: ok")
            if participant_offset is not None:
                print("  participant: reachable at offset", participant_offset)
            elif participant_error is not None:
                print("  participant: unavailable; the worker will keep retrying")
                print(" ", str(participant_error))
            else:
                print("  participant: network check skipped")
            if os.environ.get("SCANNER_ADMIN_TOKEN"):
                print("  party selection: enabled")
            else:
                print("  party selection: read-only (SCANNER_ADMIN_TOKEN is unset)")

            if args.check_only:
                print("Preflight complete.")
                return 0

            check_port(args.host, args.port)
            print(f"Starting Canton Scope at http://{args.host}:{args.port}/")
            import uvicorn

            uvicorn.run(
                "api:app",
                host=args.host,
                port=args.port,
                workers=1,
                reload=False,
            )
            return 0
    except DemoError as error:
        print(f"Demo startup blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
