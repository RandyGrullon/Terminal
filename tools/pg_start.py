"""Start an embedded PostgreSQL server for testing cloud mode locally (no Docker, no install).

pgserver ships Postgres binaries but only has wheels up to Python 3.12, so it lives in its own small venv:

    uv venv backend/.tmp/pgenv --python 3.12
    uv pip install --python backend/.tmp/pgenv/Scripts/python.exe pgserver
    backend/.tmp/pgenv/Scripts/python.exe tools/pg_start.py backend/.tmp/pgdata

It prints a connection URI and leaves the server running. Then, from backend/:

    NEXUS_TEST_PG_URL=<uri> .venv/Scripts/python.exe -m pytest -q        # whole suite on Postgres
    NEXUS_DATABASE_URL=<uri> .venv/Scripts/python.exe -m app.main         # run NEXUS on Postgres
"""
import pathlib
import sys

import pgserver  # type: ignore[import-not-found]

data_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "backend/.tmp/pgdata").resolve()
server = pgserver.get_server(data_dir, cleanup_mode=None)
print(server.get_uri())
