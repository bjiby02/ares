"""Start the server with .env loaded safely. Values may contain spaces."""
import os, runpy, pathlib
env = pathlib.Path(__file__).with_name(".env")
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
runpy.run_path(str(pathlib.Path(__file__).with_name("server.py")), run_name="__main__")
