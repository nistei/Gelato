"""Receives Webhook plugin posts and appends them to hooks.jsonl next to this file.

    python hook_listener.py [port]      (default 8765, binds 0.0.0.0)

The Linux instance reaches the host as http://host.docker.internal:<port>/hook.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks.jsonl")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_raw": raw}
        body["_received"] = time.time()
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
