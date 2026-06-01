#!/usr/bin/env python3

import builtins
import logging
import os
import time
import json
import base64
import ssl
import subprocess
import threading
from pathlib import Path
from flask import Flask, cli, request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Silenzia i log di Werkzeug (rimuove il banner e le righe per ogni richiesta)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
cli.show_server_banner = lambda *_, **__: None

app = Flask(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_CERTS_DIR = PROJECT_ROOT / "certs_external" / "server"
ENV_CERTS_DIR = Path(os.environ["CERTS_DIR"]).expanduser() if os.environ.get("CERTS_DIR") else None
CONTAINER_CERTS_DIR = Path("/certs")
SERVER_RUNTIME_LOG = Path(
    os.environ.get(
        "SERVER_RUNTIME_LOG",
        str(PROJECT_ROOT / "TreDispositivi" / "Misure" / "Runtime" / "Server.log"),
    )
).expanduser()

if ENV_CERTS_DIR is not None and ENV_CERTS_DIR.exists() and os.access(ENV_CERTS_DIR, os.W_OK):
    CERTS_DIR = ENV_CERTS_DIR
elif CONTAINER_CERTS_DIR.exists() and os.access(CONTAINER_CERTS_DIR, os.W_OK):
    CERTS_DIR = CONTAINER_CERTS_DIR
else:
    CERTS_DIR = DEFAULT_CERTS_DIR

CERT_PEM = CERTS_DIR / "cert.pem"
KEY_PEM = CERTS_DIR / "key.pem"
DC_CRED = CERTS_DIR / "dc.cred"
DC_KEY = CERTS_DIR / "dckey.pem"

GO_TOOL = os.environ.get("GO_TOOL", "/usr/local/bin/generate")
GO_FALLBACK = PROJECT_ROOT / "DC" / "go" / "bin" / "go"
GO_DC_SOURCE = PROJECT_ROOT / "DC" / "go" / "src" / "crypto" / "tls" / "generate_delegated_credential.go"

EXPERIMENT_COUNTER = 0
COUNTER_LOCK = threading.Lock()
LOG_FILE_LOCK = threading.Lock()
BASE_CERT_B64: str | None = None
BASE_KEY_B64: str | None = None


def now_ns():
    return time.time_ns()


def append_runtime_log(text: str) -> None:
    SERVER_RUNTIME_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE_LOCK:
        with SERVER_RUNTIME_LOG.open("a", encoding="utf-8") as handle:
            handle.write(text)


def runtime_print(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    message = sep.join(str(arg) for arg in args) + end
    builtins.print(*args, **kwargs)
    append_runtime_log(message)


def read_runtime_log() -> str:
    if not SERVER_RUNTIME_LOG.exists():
        return ""
    return SERVER_RUNTIME_LOG.read_text(encoding="utf-8", errors="replace")


def reset_runtime_log() -> None:
    SERVER_RUNTIME_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE_LOCK:
        SERVER_RUNTIME_LOG.write_text("", encoding="utf-8")


print = runtime_print


def has_bearer_auth(headers):
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return len(auth[7:].strip()) > 0

def run_generate_dc() -> subprocess.CompletedProcess:
    args = [
        "-cert-path", str(CERT_PEM),
        "-key-path", str(KEY_PEM),
        "-signature-scheme", "Ed25519",
        "-duration", "168h",
    ]

    go_tool_path = Path(GO_TOOL).expanduser()
    if not go_tool_path.is_absolute():
        go_tool_path = PROJECT_ROOT / go_tool_path

    if go_tool_path.exists():
        cmd = [str(go_tool_path), *args]
    elif GO_FALLBACK.exists() and GO_DC_SOURCE.exists():
        cmd = [str(GO_FALLBACK), "run", str(GO_DC_SOURCE), *args]
    else:
        missing = []
        if not go_tool_path.exists():
            missing.append(str(go_tool_path))
        if not GO_FALLBACK.exists():
            missing.append(str(GO_FALLBACK))
        if not GO_DC_SOURCE.exists():
            missing.append(str(GO_DC_SOURCE))
        raise FileNotFoundError(f"Nessun generatore DC disponibile: {', '.join(missing)}")

    return subprocess.run(
        cmd,
        cwd=CERTS_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=False,
    )


def load_static_cert_b64() -> None:
    global BASE_CERT_B64, BASE_KEY_B64
    with open(CERT_PEM, "rb") as f:
        BASE_CERT_B64 = base64.b64encode(f.read()).decode()
    with open(KEY_PEM, "rb") as f:
        BASE_KEY_B64 = base64.b64encode(f.read()).decode()


@app.get("/control/health")
def control_health():
    return {"status": "ok"}


@app.get("/control/log")
def control_log():
    return app.response_class(read_runtime_log(), mimetype="text/plain")


@app.post("/control/reset-log")
def control_reset_log():
    reset_runtime_log()
    return {"status": "ok"}


@app.post("/control/marker")
def control_marker():
    payload = request.get_json(silent=True) or {}
    marker = str(payload.get("marker", ""))
    if not marker:
        return {"error": "missing marker"}, 400
    append_runtime_log(marker if marker.endswith("\n") else marker + "\n")
    return {"status": "ok"}

@app.route("/certs", methods=["POST"])
def handle_certs():

    # record when the middlebox's request arrived (client hello)
    print(f"t4: [SERVER] - ClientHelloLatency = {now_ns()} ns")

    # extract the SNI from the request body for logging/validation
    data = request.get_json(silent=True) or {}
    sni = data.get("sni")
    print(f"[SERVER] SNI requested: {sni}")
    if not sni:
        return json.dumps({"error": "missing sni"}), 400

    # make sure we have a base certificate and key
    if not (os.path.exists(CERT_PEM) and os.path.exists(KEY_PEM)):
        return json.dumps({"error": "missing cert or key"}), 500

    print(f"t5: [SERVER] - BeginAutoGenCerts = {now_ns()} ns")   
    # generate a fresh delegated credential; fail loudly if the helper returns an error
    result = run_generate_dc()
    if result.returncode != 0:
        # include stderr to help debugging
        err = (result.stderr or b"").decode(errors="replace")
        print("[SERVER] generate tool failed:", err)
        return json.dumps({"error": "dc generation failed"}), 500

    # read generated files and base cert/key, always encode as strings
    with open(DC_CRED, "rb") as f:
        dc_cred_b64 = base64.b64encode(f.read()).decode()
    with open(DC_KEY, "rb") as f:
        dc_key_b64 = base64.b64encode(f.read()).decode()
    cert_b64 = BASE_CERT_B64 or ""
    key_b64 = BASE_KEY_B64 or ""

    print(f"t6: [SERVER] - EndAutoGenCerts = {now_ns()} ns")

    # attach timestamps as strings for optional debugging in the response
    response = {
        "cert_b64": cert_b64,
        "key_b64": key_b64,
        "dc_cred_b64": dc_cred_b64,
        "dc_key_b64": dc_key_b64,
    }

    print(f"t7: [SERVER] - Sending to middlebox: {now_ns()} ns")

    return json.dumps(response), 200, {"Content-Type": "application/json"}


# =========================
# APPLICATION SERVER 8000
# =========================

class AppHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        t_app_1 = now_ns()
        print(f"t25: [SERVER APP] RequestReceived = {t_app_1} ns")

        if self.path == "/function/init":
            if not has_bearer_auth(self.headers):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "missing or invalid bearer token"}).encode())
                return

            response = {
                "status": "ok",
                "message": "function initialized"
            }

            data = json.dumps(response).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

        t_app_2 = now_ns()
        print(f"t26: [SERVER APP] ResponseSent = {t_app_2} ns")

    def do_POST(self):

        t_app_1 = now_ns()
        print(f"t25: [SERVER APP] RequestReceived = {t_app_1} ns")

        if self.path == "/function/init":
            if not has_bearer_auth(self.headers):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "missing or invalid bearer token"}).encode())
                return

            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length > 0 else b""
            print(f"[SERVER APP] POST body: {body}")

            response = {
                "status": "ok",
                "message": "function initialized"
            }
            data = json.dumps(response).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

        t_app_2 = now_ns()
        print(f"t26: [SERVER APP] ResponseSent = {t_app_2} ns")


def run_app_server():
    # Attende che i certificati siano disponibili prima di avviare il listener TLS.
    while not (os.path.exists(CERT_PEM) and os.path.exists(KEY_PEM)):
        time.sleep(0.5)

    server = ThreadingHTTPServer(("0.0.0.0", 8000), AppHandler)
    server.request_queue_size = 1024  # Backlog per alte frequenze
    server.allow_reuse_address = True  # Consente rapid reconnect

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT_PEM, keyfile=KEY_PEM)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    print("[SERVER] Application TLS server running on :8000")
    server.serve_forever()


if __name__ == "__main__":

    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_RUNTIME_LOG.parent.mkdir(parents=True, exist_ok=True)

    # Carica una volta sola cert/key in base64: sono statici tra richieste.
    if os.path.exists(CERT_PEM) and os.path.exists(KEY_PEM):
        load_static_cert_b64()

    threading.Thread(target=run_app_server, daemon=True).start()

    print("[SERVER] cert service listening on :5000")
    app.run(host="0.0.0.0", port=5000)
