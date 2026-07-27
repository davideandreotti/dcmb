#!/usr/bin/env python3

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class InitHandler(BaseHTTPRequestHandler):
    server_version = "InitHTTP/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/control/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        print("[INIT HTTP] RequestReceived", flush=True)
        if self.path != "/function/init":
            self._send_json(404, {"error": "not found"})
            return

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            self._send_json(401, {"error": "missing or invalid bearer token"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        print(f"[INIT HTTP] POST /function/init body={body!r}", flush=True)

        self._send_json(200, {"status": "ok", "message": "function initialized"})
        print("[INIT HTTP] ResponseSent", flush=True)


def main():
    port = int(os.environ.get("TLMSP_INIT_BACKEND_PORT", "7000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), InitHandler)
    server.allow_reuse_address = True
    print(f"[INIT HTTP] listening on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
