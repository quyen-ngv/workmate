"""A tiny app the UI preview tests start as the project's ui.seed / ui.start commands (SI-23).

    ui_fake_app.py seed [args…]   writes seed.json in AO_UI_DATA
    ui_fake_app.py serve [args…]  writes serve.json, then answers HTTP on 127.0.0.1:AO_UI_PORT
    ui_fake_app.py crash          prints its token and exits 3 (the app does not come up)
    ui_fake_app.py silent         never listens (the URL never answers)
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = b"""<!doctype html><html><head><title>fake</title></head><body>
<h1>Trang thu</h1>
<input id="name"><button id="go" onclick="document.getElementById('out').textContent = document.getElementById('name').value">Go</button>
<div id="out"></div>
<img src="http://example.com/outside.png" alt="outside">
<script>fetch("http://example.com/beacon").catch(() => {});</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *_args: object) -> None:
        return


def main() -> int:
    mode = sys.argv[1]
    data = Path(os.environ["AO_UI_DATA"])
    if mode == "seed":
        (data / "seed.json").write_text(json.dumps({"cwd": os.getcwd(), "argv": sys.argv[2:]}), encoding="utf-8")
        return 0
    if mode == "crash":
        print("starting with token " + os.environ["AO_UI_TOKEN"], flush=True)
        print("boom: port already in use", flush=True)
        return 3
    if mode == "silent":
        time.sleep(300)
        return 0
    port = int(os.environ["AO_UI_PORT"])
    info = {"port": port, "token": os.environ["AO_UI_TOKEN"], "pid": os.getpid(), "argv": sys.argv[2:], "seeded": (data / "seed.json").is_file()}
    (data / "serve.json").write_text(json.dumps(info), encoding="utf-8")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
