"""Start the server with .env loaded safely. Values may contain spaces."""
import os, runpy, pathlib


def _plain(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return value


env = pathlib.Path(__file__).with_name(".env")
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = _plain(value)
        key = key.strip()
        current = os.environ.get(key, "")
        # A quoted value inherited from the shell is not a real override.
        if not current.strip() or _plain(current) != current.strip():
            os.environ[key] = value
import socket

PORT = 8810
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
busy = probe.connect_ex(("127.0.0.1", PORT)) == 0
probe.close()
if busy:
    print(f"\nPort {PORT} is already serving.\n")
    print("  Something is running there already — most likely an older copy of ARES.")
    print("  Free it and start again:\n")
    print(f"    lsof -ti:{PORT} | xargs kill\n")
    raise SystemExit(1)

runpy.run_path(str(pathlib.Path(__file__).with_name("server.py")), run_name="__main__")
