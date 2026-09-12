"""A loopback Anki sync server backed only by a disposable test directory."""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


@contextmanager
def native_sync_server(directory):
    root = Path(directory) / "native-sync"
    root.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SYNC_")}
    environment.update(SYNC_HOST="127.0.0.1", SYNC_PORT=str(port), SYNC_BASE=str(root / "server"),
                       SYNC_USER1="probe:disposable", RUST_LOG="error")
    environment.pop("PASSWORDS_HASHED", None)
    with (root / "server.log").open("w+") as log:
        server = subprocess.Popen([sys.executable, "-m", "anki.syncserver"], env=environment, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                if server.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    raise AssertionError(f"Disposable sync server did not start: {log.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            yield endpoint
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def full_sync(collection, auth, *, upload):
    collection.close_for_full_sync()
    try:
        collection.full_upload_or_download(auth=auth, server_usn=None, upload=upload)
    finally:
        collection.reopen(after_full_sync=True)
