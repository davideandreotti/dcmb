#!/usr/bin/env python3
from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shlex
import socket
import statistics
import subprocess
import threading
import time
from pathlib import Path

# Array di rate (ms tra richieste)
RATES = [25, 20, 25, 10, 7, 5, 2]

# Finestra temporale (secondi) entro cui inviare tutte le richieste possibili per ogni rate
TIME_REQUESTS_SECONDS = 20

# Scenario: 1 = middlebox reset/re-auth, 2 = middlebox cache, 3 = gateway+operator orchestrati
SCENARIO = 2

# Docker network per client esterno che invia richieste ai NodePort Kubernetes
DOCKER_NETWORK_NAME = "misure-kubernetes"

# Kubernetes
NAMESPACE = "default"
SINGLE_MIDDLEBOX_POD = "middlebox-throughput"
SINGLE_MIDDLEBOX_APP_LABEL = "middlebox-throughput"
SINGLE_MIDDLEBOX_SERVICE = "middlebox-throughput-svc"
SINGLE_MIDDLEBOX_PORT = 8443
MB_LOG_PATH_IN_POD = "/tmp/Middlebox.log"

SERVER_K8S_SERVICE = "server"  # deve corrispondere a http://server:5000 hardcoded in middlebox.go

OPERATOR_DEPLOYMENT = "middlebox-operator-kubernetes"
GATEWAY_DEPLOYMENT = "middlebox-gateway-kubernetes"
GATEWAY_SERVICE = "middlebox-kubernetes"

# Parametri orchestrazione scenario 3
INITIAL_OPERATORS = 10
MIN_READY_OPERATORS = 7
SCALE_UP_BY = 7
AUTOSCALE_RATE_MS = 100
NO_READY_OPERATOR_POLICY = "wait"
NO_READY_OPERATOR_WAIT_SECONDS = 120

SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR / "PerformanceMeasuring").exists() and (SCRIPT_DIR / "DC/Middlebox").exists():
    PROJECT_ROOT = SCRIPT_DIR
elif (SCRIPT_DIR / "MasterThesis/PerformanceMeasuring").exists():
    PROJECT_ROOT = SCRIPT_DIR / "MasterThesis"
else:
    PROJECT_ROOT = SCRIPT_DIR

SERVER_DIR = PROJECT_ROOT / "PerformanceMeasuring"
MB_DIR = PROJECT_ROOT / "DC/Middlebox"
GO_PROFESSOR = PROJECT_ROOT / "DC/go/bin/go"
CLIENT_BINARY = MB_DIR / "client"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"

TOKEN = "token"

BASE_DIR = PROJECT_ROOT / "ThroughputKubernetes"
RESULTS_DIR = BASE_DIR / "Risultati"
RUNTIME_DIR = BASE_DIR / "Runtime"
GRAFICI_DIR = BASE_DIR / "Grafici"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"

STARTUP_TIMEOUT = 60

# Timeout richieste client
CLIENT_REQUEST_TIMEOUT_SECONDS = 120
MAX_CLIENT_WORKERS = 128
# Baseline service time (t10-t1 medio da MisureKubernetes/Analisi/Risultati.txt)
BASELINE_SERVICE_TIME_MS = {
    1: 13.10,  # operazione_N_2: t10-t1 average = 13098748 ns
    2: 4.90,   # operazione_N_3: t10-t1 average = 4902210 ns
    3: 13.10,  # Scenario 3: uguale a scenario 1 (gateway+operator)
}
TIMESTAMP_PATTERN = re.compile(r"\bt(\d+)\b\s*:\s*[^\n\r]*?=\s*(\d+)")

_RUN_ENV_CACHE: dict[str, str] | None = None
_NODE_HOST_CACHE: str | None = None
CURRENT_REFILL_PERIOD_MS = AUTOSCALE_RATE_MS


def ensure_client_binary() -> None:
    if CLIENT_BINARY.exists() and CLIENT_BINARY.stat().st_mtime >= (MB_DIR / "client.go").stat().st_mtime:
        return
    if not GO_PROFESSOR.exists():
        raise FileNotFoundError(f"Compilatore Go del professore non trovato: {GO_PROFESSOR}")
    result = subprocess.run(
        [str(GO_PROFESSOR), "build", "-o", str(CLIENT_BINARY), "client.go"],
        cwd=str(MB_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        raise RuntimeError(f"Impossibile compilare il client locale:\n{output}")


def cleanup_stale_server_processes() -> None:
    subprocess.run(
        "pkill -f certs_server.py",
        shell=True,
        capture_output=True,
        text=True,
        env=get_run_env(),
    )
    time.sleep(0.2)


def get_local_port_conflict_diagnostics() -> str:
    return run(
        "ss -ltnp | awk 'NR==1 || $4 ~ /:5000$/ || $4 ~ /:8000$/'",
        check=False,
    ).strip()


def start_server_process() -> subprocess.Popen[str]:
    cleanup_stale_server_processes()
    server_log = SERVER_RUNTIME_LOG.open("w", encoding="utf-8")
    process = subprocess.Popen(
        ["python3", "-u", str(SERVER_SCRIPT)],
        cwd=str(SERVER_DIR),
        stdout=server_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + STARTUP_TIMEOUT
    marker = "[SERVER] Application TLS server running on :8000"
    while time.time() < deadline:
        content = read_text(SERVER_RUNTIME_LOG)
        if marker in content:
            server_log.close()
            return process
        if process.poll() is not None:
            server_log.close()
            diagnostics = ""
            if "Address already in use" in content or "is in use by another program" in content:
                details = get_local_port_conflict_diagnostics()
                if details:
                    diagnostics = f"\nDiagnostica porte locali (5000/8000):\n{details}\n"
            raise RuntimeError(
                f"Il server locale è terminato inaspettatamente. Log:\n{content[-2000:]}{diagnostics}"
            )
        time.sleep(0.1)

    server_log.close()
    process.terminate()
    details = get_local_port_conflict_diagnostics()
    extra = f"\nDiagnostica porte locali (5000/8000):\n{details}" if details else ""
    raise TimeoutError(f"Marker non trovato nel log server: {marker}{extra}")


def stop_server_process(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_client_process(command: list[str], timeout: int) -> str:
    completed = subprocess.run(
        command,
        cwd=str(MB_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return (completed.stdout or "") + (completed.stderr or "")


def build_client_command(target: str) -> list[str]:
    url = f"https://{target}/function/init"
    return [
        str(CLIENT_BINARY),
        "-ca",
        str(CLIENT_CA_HOST_PATH),
        "-servername",
        "server",
        "-H",
        f"Authorization : Bearer {TOKEN}",
        url,
    ]


def detect_baremetal_server_endpoint_ip() -> str:
    override = os.environ.get("BAREMETAL_SERVER_ENDPOINT_IP")
    if override:
        return override
    context = run("kubectl config current-context", check=False).strip()
    if context.startswith("kind-"):
        probe = run(
            "docker exec kind-control-plane sh -lc \"getent hosts host.docker.internal | awk '{print $1; exit}'\"",
            check=False,
        ).strip()
        parsed_probe = extract_ip_address(probe)
        if parsed_probe:
            return parsed_probe
        probe = run(
            "docker exec kind-control-plane sh -lc \"ip route | awk '/default/ {print $3; exit}'\"",
            check=False,
        ).strip()
        parsed_probe = extract_ip_address(probe)
        if parsed_probe:
            return parsed_probe
    return "127.0.0.1"


def compute_requests_for_rate(rate_ms: int) -> int:
    window_ms = int(TIME_REQUESTS_SECONDS * 1000)
    return max(1, window_ms // rate_ms)


############################################
# UTILITA GENERALI
############################################

def ensure_directories() -> None:
    for directory in (BASE_DIR, RESULTS_DIR, RUNTIME_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def cleanup_old_output_files() -> None:
    """Rimuove CSV vecchi da Risultati/ e svuota la cartella Grafici/ prima di ogni run."""
    if RESULTS_DIR.exists():
        for f in RESULTS_DIR.iterdir():
            if f.is_file():
                f.unlink()
        print(f"[CLEANUP] Rimossi file vecchi da {RESULTS_DIR}")
    if GRAFICI_DIR.exists():
        import shutil as _shutil
        _shutil.rmtree(GRAFICI_DIR)
        print(f"[CLEANUP] Svuotata cartella {GRAFICI_DIR}")
    GRAFICI_DIR.mkdir(parents=True, exist_ok=True)


def reset_runtime_logs() -> None:
    for path in (CLIENT_RUNTIME_LOG, MIDDLEBOX_RUNTIME_LOG, SERVER_RUNTIME_LOG):
        if path.exists():
            path.unlink()


def quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def _probe_docker_env(env: dict[str, str]) -> bool:
    result = subprocess.run(
        "docker info --format '{{.ServerVersion}}'",
        shell=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode == 0


def get_run_env() -> dict[str, str]:
    global _RUN_ENV_CACHE
    if _RUN_ENV_CACHE is not None:
        return _RUN_ENV_CACHE

    env = os.environ.copy()

    if env.get("DOCKER_HOST"):
        _RUN_ENV_CACHE = env
        return _RUN_ENV_CACHE

    if _probe_docker_env(env):
        _RUN_ENV_CACHE = env
        return _RUN_ENV_CACHE

    fallback_socket = Path("/var/run/docker.sock")
    if fallback_socket.exists():
        fallback_env = env.copy()
        fallback_env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        if _probe_docker_env(fallback_env):
            _RUN_ENV_CACHE = fallback_env
            return _RUN_ENV_CACHE

    _RUN_ENV_CACHE = env
    return _RUN_ENV_CACHE


def run(
    command: str,
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> str:
    print(f"EXEC: {command}")
    result = subprocess.run(
        command,
        shell=True,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=get_run_env(),
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Comando fallito con codice {result.returncode}: {command}\n{output}"
        )
    return output


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def write_results_csv(results: list[dict], raw_rows: list[dict], scenario: int) -> None:
    csv_path = RESULTS_DIR / f"Throughput_Scenario_{scenario}.csv"
    if results:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"\n✓ Risultati salvati: {csv_path}")

    raw_csv_path = RESULTS_DIR / f"Throughput_Scenario_{scenario}_raw.csv"
    if raw_rows:
        with open(raw_csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=raw_rows[0].keys())
            writer.writeheader()
            writer.writerows(raw_rows)
        print(f"✓ Latenze raw salvate: {raw_csv_path}")


def extract_ip_address(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line)
        if match:
            return match.group(0)
    return None


def ensure_kubernetes_cluster_ready() -> None:
    current_ctx_proc = subprocess.run(
        "kubectl config current-context",
        shell=True,
        capture_output=True,
        text=True,
    )
    current_context = (current_ctx_proc.stdout or "").strip()
    if current_ctx_proc.returncode != 0 or not current_context:
        contexts = run("kubectl config get-contexts -o name", check=False).strip()
        available = contexts if contexts else "(nessuno)"
        raise RuntimeError(
            "Nessun Kubernetes context attivo.\n"
            f"Context disponibili: {available}\n"
            "Imposta il contesto con: kubectl config use-context <nome>"
        )

    api_probe = subprocess.run(
        "kubectl --request-timeout=5s get --raw=/readyz",
        shell=True,
        capture_output=True,
        text=True,
    )
    if api_probe.returncode != 0 or "ok" not in (api_probe.stdout or "").lower():
        details = ((api_probe.stdout or "") + (api_probe.stderr or "")).strip()
        raise RuntimeError(
            f"API server Kubernetes non raggiungibile (contesto: {current_context}).\n"
            f"Dettagli: {details}\n"
            "Verifica con: kubectl cluster-info && kubectl get nodes"
        )


def maybe_load_kind_images(include_orchestration: bool = False) -> None:
    context = run("kubectl config current-context", check=False).strip()
    if not context.startswith("kind-"):
        return

    kind_cluster_name = context.removeprefix("kind-") or "kind"
    images = ["middlebox:latest"]
    if include_orchestration:
        images += ["mb_gateway:latest", "mb_operator:latest"]
    for image in images:
        run(f"kind load docker-image {quote(image)} --name {quote(kind_cluster_name)}", check=False)


def ensure_docker_network() -> None:
    result = subprocess.run(
        f"docker network inspect {quote(DOCKER_NETWORK_NAME)}",
        shell=True,
        capture_output=True,
        text=True,
        env=get_run_env(),
    )
    if result.returncode != 0:
        run(f"docker network create {quote(DOCKER_NETWORK_NAME)}")


def get_node_host() -> str:
    override = os.environ.get("K8S_NODEPORT_HOST")
    if override:
        return override

    context = run("kubectl config current-context", check=False).strip()
    if context.startswith("kind-"):
        probe = run(
            "kubectl get node kind-control-plane -o jsonpath='{.status.addresses[?(@.type==\"InternalIP\")].address}'",
            check=False,
        ).strip().strip("'")
        ip = extract_ip_address(probe)
        if ip:
            return ip

        probe = run(
            "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' kind-control-plane",
            check=False,
        ).strip()
        ip = extract_ip_address(probe)
        if ip:
            return ip

    return "127.0.0.1"


def wait_for_nodeport_reachable(host: str, port: int, service_name: str, timeout_s: int = 60) -> None:
    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.5)

    svc = run(
        f"kubectl -n {quote(NAMESPACE)} get svc {quote(service_name)} -o wide",
        check=False,
    )
    ep = run(
        f"kubectl -n {quote(NAMESPACE)} get endpoints {quote(service_name)} -o yaml",
        check=False,
    )
    raise RuntimeError(
        f"NodePort non raggiungibile su {host}:{port} dopo {timeout_s}s (last_error={last_error}).\n"
        f"Service:\n{svc[-2000:]}\n\nEndpoints:\n{ep[-3000:]}"
    )


############################################
# BUILD/SETUP
############################################

def build_images(include_orchestration: bool = False) -> None:
    print(f"!!! BUILD IMAGES - START : {time.time_ns()}")
    run(
        f"docker build -f {quote(MB_DIR / 'Dockerfile.middlebox')} -t middlebox .",
        cwd=PROJECT_ROOT,
    )
    if include_orchestration:
        run(
            f"docker build -f {quote(MB_DIR / 'Dockerfile.gateway')} -t mb_gateway:latest .",
            cwd=PROJECT_ROOT,
        )
        run(
            f"docker build -f {quote(MB_DIR / 'Dockerfile.operator')} -t mb_operator:latest .",
            cwd=PROJECT_ROOT,
        )
    print(f"!!! BUILD IMAGES - END : {time.time_ns()}")


def stop_swarm_client_container() -> None:
    return


############################################
# KUBERNETES - SCENARIO 1/2 (MIDDLEBOX SINGOLO)

# Client e server restano processi baremetal locali.
# Nel cluster pubblichiamo solo un bridge Service+Endpoints verso il server locale.
def delete_server_bridge() -> None:
    run(
                f"kubectl -n {quote(NAMESPACE)} delete svc {quote(SERVER_K8S_SERVICE)} --ignore-not-found=true",
        check=False,
    )
    run(
                f"kubectl -n {quote(NAMESPACE)} delete endpoints {quote(SERVER_K8S_SERVICE)} --ignore-not-found=true",
        check=False,
    )


def create_server_bridge() -> None:
    delete_server_bridge()
    endpoint_ip = detect_baremetal_server_endpoint_ip()
    service_manifest = f"""
apiVersion: v1
kind: Service
metadata:
  name: {SERVER_K8S_SERVICE}
  namespace: {NAMESPACE}
spec:
  ports:
  - name: certs
    port: 5000
    protocol: TCP
  - name: app-tls
    port: 8000
    protocol: TCP
""".strip()
    endpoints_manifest = f"""
apiVersion: v1
kind: Endpoints
metadata:
  name: {SERVER_K8S_SERVICE}
  namespace: {NAMESPACE}
subsets:
  - addresses:
      - ip: {endpoint_ip}
    ports:
      - name: certs
        port: 5000
        protocol: TCP
      - name: app-tls
        port: 8000
        protocol: TCP
""".strip()
    run("\n".join(["cat <<'YAML' | kubectl apply -f -", service_manifest, "---", endpoints_manifest, "YAML"]))

############################################

def ensure_nodeport_service(service_name: str) -> None:
    output = run(
        f"kubectl -n {quote(NAMESPACE)} get svc {quote(service_name)} "
        "-o jsonpath='{.spec.ports[0].nodePort}'",
        check=False,
    ).strip().strip("'")
    if output:
        return

    run(
        f"kubectl -n {quote(NAMESPACE)} patch svc {quote(service_name)} "
        "-p '{\"spec\":{\"type\":\"NodePort\"}}'",
        check=True,
    )


def get_service_nodeport(service_name: str, timeout_s: int = 60) -> int:
    started = time.time()
    while True:
        output = run(
            f"kubectl -n {quote(NAMESPACE)} get svc {quote(service_name)} "
            "-o jsonpath='{.spec.ports[0].nodePort}'",
            check=False,
        ).strip().strip("'")
        if output:
            return int(output)
        if time.time() - started > timeout_s:
            raise RuntimeError(f"Impossibile ottenere NodePort del service {service_name}")
        time.sleep(0.5)


def wait_service_endpoints(service_name: str, timeout_s: int = 60) -> None:
    started = time.time()
    while True:
        output = run(
            f"kubectl -n {quote(NAMESPACE)} get endpoints {quote(service_name)} "
            "-o jsonpath='{.subsets[*].addresses[*].ip}'",
            check=False,
        ).strip().strip("'")
        if output:
            return
        if time.time() - started > timeout_s:
            endpoints_yaml = run(
                f"kubectl -n {quote(NAMESPACE)} get endpoints {quote(service_name)} -o yaml",
                check=False,
            )
            raise TimeoutError(
                f"Service {service_name} senza endpoint pronti dopo {timeout_s}s.\n{endpoints_yaml}"
            )
        time.sleep(0.5)


def delete_single_middlebox_pod() -> None:
    run(
        f"kubectl -n {quote(NAMESPACE)} delete pod {quote(SINGLE_MIDDLEBOX_POD)} "
        "--ignore-not-found=true --wait=true",
        check=False,
    )


def create_single_middlebox_pod() -> None:
    delete_single_middlebox_pod()
    run(
        f"kubectl -n {quote(NAMESPACE)} run {quote(SINGLE_MIDDLEBOX_POD)} "
        "--image=middlebox "
        "--restart=Never "
        "--image-pull-policy=IfNotPresent "
        f"--labels app={quote(SINGLE_MIDDLEBOX_APP_LABEL)} "
        "-- tail -f /dev/null",
        check=True,
    )
    run(
        f"kubectl -n {quote(NAMESPACE)} wait --for=condition=Ready "
        f"pod/{quote(SINGLE_MIDDLEBOX_POD)} --timeout=180s",
        check=True,
    )
    run(
        f"kubectl -n {quote(NAMESPACE)} expose pod {quote(SINGLE_MIDDLEBOX_POD)} "
        f"--name={quote(SINGLE_MIDDLEBOX_SERVICE)} --type=NodePort "
        f"--port={SINGLE_MIDDLEBOX_PORT} --target-port={SINGLE_MIDDLEBOX_PORT}",
        check=False,
    )


def get_pod_log_content_raw() -> str:
    return run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(SINGLE_MIDDLEBOX_POD)} -- "
        f"cat {quote(MB_LOG_PATH_IN_POD)}",
        check=False,
    )


def wait_for_marker_in_pod(marker: str, start_offset: int = 0) -> None:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        content = get_pod_log_content_raw()
        if marker in content[start_offset:]:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Marker non trovato nel log pod {SINGLE_MIDDLEBOX_POD}: {marker}")


def stop_service_in_pod(process_pattern: str) -> None:
    kill_command = (
        f"pkill -INT -f {shlex.quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -TERM -f {shlex.quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -KILL -f {shlex.quote(process_pattern)} 2>/dev/null || true"
    )
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(SINGLE_MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(kill_command)}",
        check=False,
    )


def start_middlebox_in_pod(force_regenerate_each_request: bool = False) -> None:
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(SINGLE_MIDDLEBOX_POD)} -- "
        f"sh -lc 'mkdir -p /tmp && touch {quote(MB_LOG_PATH_IN_POD)}'",
        check=False,
    )
    current_size = len(get_pod_log_content_raw())
    if force_regenerate_each_request:
        inner_cmd = f"cd /app && MB_FORCE_REGENERATE_DC_EACH_REQUEST=1 ./middlebox >> {MB_LOG_PATH_IN_POD} 2>&1 &"
    else:
        inner_cmd = f"cd /app && ./middlebox >> {MB_LOG_PATH_IN_POD} 2>&1 &"
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(SINGLE_MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(inner_cmd)}",
        check=True,
    )
    wait_for_marker_in_pod("[MB] Middlebox listening on :8443", start_offset=current_size)


############################################
# KUBERNETES - SCENARIO 3 (GATEWAY + OPERATOR)
############################################

def deploy_orchestrate_kubernetes_stack() -> None:
    manifest = MB_DIR / "orchestrate_kubernetes.yaml"
    run(f"kubectl -n {quote(NAMESPACE)} apply -f {quote(manifest)}", check=True)
    run(f"kubectl -n {quote(NAMESPACE)} scale deployment/client-kubernetes --replicas=0", check=False)

    rollout_targets = ["server-kubernetes", OPERATOR_DEPLOYMENT, GATEWAY_DEPLOYMENT]
    for name in rollout_targets:
        run(
            f"kubectl -n {quote(NAMESPACE)} rollout status deployment/{quote(name)} --timeout=300s",
            check=True,
        )


def cleanup_orchestrate_kubernetes_stack() -> None:
    manifest = MB_DIR / "orchestrate_kubernetes.yaml"
    run(
        f"kubectl -n {quote(NAMESPACE)} delete -f {quote(manifest)} --ignore-not-found=true --wait=false",
        check=False,
    )


def get_middlebox_replica_snapshot() -> tuple[int, int, int, int]:
    deploy_json = run(
        f"kubectl -n {quote(NAMESPACE)} get deployment {quote(OPERATOR_DEPLOYMENT)} -o json",
        check=False,
    )
    try:
        parsed = json.loads(deploy_json) if deploy_json.strip() else {}
    except json.JSONDecodeError:
        parsed = {}

    spec = parsed.get("spec") if isinstance(parsed, dict) else {}
    status = parsed.get("status") if isinstance(parsed, dict) else {}
    desired = int((spec or {}).get("replicas") or 0)
    ready = int((status or {}).get("readyReplicas") or 0)
    available = int((status or {}).get("availableReplicas") or 0)
    pending = max(0, desired - ready)
    return ready, available, desired, pending


def scale_middlebox(new_replicas: int) -> None:
    run(
        f"kubectl -n {quote(NAMESPACE)} scale deployment/{quote(OPERATOR_DEPLOYMENT)} "
        f"--replicas={max(0, new_replicas)}",
        check=True,
    )


def set_refill_period_for_rate(rate_ms: int) -> int:
    global CURRENT_REFILL_PERIOD_MS
    CURRENT_REFILL_PERIOD_MS = max(1, int(rate_ms * 0.45))
    return CURRENT_REFILL_PERIOD_MS


def wait_middlebox_pool_ready(min_ready: int) -> int:
    while True:
        ready, available, desired, pending = get_middlebox_replica_snapshot()
        print(
            f"[SCENARIO3][POOL] ready={ready} available={available} desired={desired} pending={pending} target>={min_ready}"
        )
        if ready >= min_ready:
            return ready
        time.sleep(1)


def run_k8s_refill_loop(stop_event: threading.Event) -> None:
    period = max(CURRENT_REFILL_PERIOD_MS, 1) / 1000.0
    print(
        f"[SCENARIO3] refill-loop start: interval dinamico = 45% del rate "
        f"(iniziale {CURRENT_REFILL_PERIOD_MS}ms), min_ready={MIN_READY_OPERATORS}, scale_by={SCALE_UP_BY}"
    )

    while not stop_event.is_set():
        try:
            ready, _available, desired, _pending = get_middlebox_replica_snapshot()
            if ready < MIN_READY_OPERATORS:
                target = desired + SCALE_UP_BY
                print(
                    f"[SCENARIO3][REFILL] ready={ready} desired={desired} "
                    f"< min_ready={MIN_READY_OPERATORS} -> scale to {target}"
                )
                scale_middlebox(target)
        except Exception as exc:
            print(f"[SCENARIO3][REFILL] warning: {exc}")

        period = max(CURRENT_REFILL_PERIOD_MS, 1) / 1000.0
        stop_event.wait(period)

    print("[SCENARIO3] refill-loop stop")


############################################
# THROUGHPUT TEST
############################################

def run_throughput_test_scenario_1_or_2(scenario: int, rate_ms: int, target: str) -> dict:
    reset_runtime_logs()
    total_requests = compute_requests_for_rate(rate_ms)

    latencies_by_req: list[int | None] = [None] * total_requests
    queue_delay_by_req_ms: list[float | None] = [None] * total_requests
    dispatch_delay_by_req_ms: list[float | None] = [None] * total_requests
    status_by_req: list[str] = ["pending"] * total_requests
    planned_send_time_by_req: list[float | None] = [None] * total_requests
    results_lock = threading.Lock()

    request_timeout = CLIENT_REQUEST_TIMEOUT_SECONDS

    def fire_request(req_num: int) -> None:
        req_index = req_num - 1
        url = f"https://{target}/function/init"
        client_cmd = f"cd /app && ./client -H 'Authorization : Bearer {TOKEN}' {url}"

        planned = planned_send_time_by_req[req_index]
        now = time.perf_counter()
        dispatch_delay_ms = 0.0
        if planned is not None:
            dispatch_delay_ms = max(0.0, (now - planned) * 1000.0)
        with results_lock:
            dispatch_delay_by_req_ms[req_index] = dispatch_delay_ms
        print(
            f"  --> Richiesta {req_num}/{total_requests} inviata "
            f"(dispatch_delay={dispatch_delay_ms:.2f}ms, client=baremetal, t={time.time():.3f})"
        )

        try:
            output = run_client_process(build_client_command(target), timeout=request_timeout)
        except subprocess.TimeoutExpired:
            print(
                f"  <-- Richiesta {req_num} timeout client ({request_timeout}s) "
                f"verso {target}"
            )
            with results_lock:
                status_by_req[req_index] = "timeout"
            return
        except Exception as exc:
            print(f"  <-- Richiesta {req_num} errore esecuzione client: {exc}")
            with results_lock:
                status_by_req[req_index] = "error"
            return

        append_text(CLIENT_RUNTIME_LOG, output)

        t1_all = re.findall(r"t1: \[CLIENT\][^=]*= (\d+) ns", output)
        t10_all = re.findall(r"t10: \[CLIENT\][^=]*= (\d+) ns", output)
        if t1_all and t10_all:
            lat = int(t10_all[-1]) - int(t1_all[-1])
            if lat > 0:
                service_ms = lat / 1_000_000
                baseline_ms = BASELINE_SERVICE_TIME_MS.get(scenario, 11.0)
                queue_ms = max(0.0, service_ms - baseline_ms)
                with results_lock:
                    latencies_by_req[req_index] = lat
                    queue_delay_by_req_ms[req_index] = queue_ms
                    status_by_req[req_index] = "ok"
                print(
                    f"  <-- Richiesta {req_num} completata: "
                    f"servizio={service_ms:.2f}ms, coda={queue_ms:.2f}ms, dispatch_delay={dispatch_delay_ms:.2f}ms"
                )
            else:
                with results_lock:
                    status_by_req[req_index] = "invalid_latency"
        else:
            if "Client: Error during request:" in output:
                print(f"  <-- Richiesta {req_num} client_error output:\n{output[:600]}")
                if req_num <= 3:
                    mb_tail = run(
                        f"kubectl -n {quote(NAMESPACE)} exec {quote(SINGLE_MIDDLEBOX_POD)} -- "
                        f"sh -lc 'tail -n 60 {quote(MB_LOG_PATH_IN_POD)}'",
                        check=False,
                    )
                    if mb_tail.strip():
                        print(f"  <-- Richiesta {req_num} middlebox log tail:\n{mb_tail[:1200]}")
                with results_lock:
                    status_by_req[req_index] = "client_error"
            else:
                print(f"  <-- Richiesta {req_num} no_timestamp output:\n{output[:600]}")
                with results_lock:
                    status_by_req[req_index] = "no_timestamp"

    interval_s = rate_ms / 1000.0
    schedule_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=MAX_CLIENT_WORKERS) as executor:
        futures = []
        for i in range(total_requests):
            planned = schedule_start + i * interval_s
            planned_send_time_by_req[i] = planned

            while True:
                remaining = planned - time.perf_counter()
                if remaining <= 0:
                    break
                if remaining > 0.002:
                    time.sleep(min(remaining / 2.0, 0.001))

            futures.append(executor.submit(fire_request, i + 1))

        for future in futures:
            future.result()

    latencies = [value for value in latencies_by_req if value is not None]
    dispatch_delays_ok = [dispatch_delay_by_req_ms[i] for i in range(total_requests) if status_by_req[i] == "ok" and dispatch_delay_by_req_ms[i] is not None]
    queue_delays_ok = [queue_delay_by_req_ms[i] for i in range(total_requests) if status_by_req[i] == "ok" and queue_delay_by_req_ms[i] is not None]
    missing = total_requests - len(latencies)
    missing_timeout = sum(1 for s in status_by_req if s == "timeout")
    missing_no_timestamp = sum(1 for s in status_by_req if s == "no_timestamp")
    missing_client_error = sum(1 for s in status_by_req if s == "client_error")
    missing_other = sum(1 for s in status_by_req if s in ("error", "invalid_latency", "pending"))

    print(f"  Rate {rate_ms}ms: {len(latencies)}/{total_requests} latenze raccolte")
    if missing > 0:
        print(
            f"  Avviso missing={missing}: timeout={missing_timeout}, "
            f"client_error={missing_client_error}, no_timestamp={missing_no_timestamp}, other={missing_other}"
        )

    return {
        "rate_ms": rate_ms,
        "scenario": scenario,
        "latencies": latencies,
        "dispatch_delays_ms": dispatch_delays_ok,
        "queue_delays_ms": queue_delays_ok,
        "status_by_req": status_by_req,
        "service_latency_by_req_ns": latencies_by_req,
        "dispatch_delay_by_req_ms": dispatch_delay_by_req_ms,
        "queue_delay_by_req_ms": queue_delay_by_req_ms,
        "total_requests": total_requests,
        "missing": missing,
        "missing_timeout": missing_timeout,
        "missing_client_error": missing_client_error,
        "missing_no_timestamp": missing_no_timestamp,
        "missing_other": missing_other,
    }


def compute_stats(latencies: list[int]) -> dict:
    if not latencies:
        return {
            "count": 0,
            "mean_ms": 0,
            "median_ms": 0,
            "std_ms": 0,
            "min_ms": 0,
            "max_ms": 0,
        }

    latencies_ms = [lat / 1_000_000 for lat in latencies]
    return {
        "count": len(latencies),
        "mean_ms": statistics.fmean(latencies_ms),
        "median_ms": statistics.median(latencies_ms),
        "std_ms": statistics.stdev(latencies_ms) if len(latencies_ms) > 1 else 0,
        "min_ms": min(latencies_ms),
        "max_ms": max(latencies_ms),
    }


def run_throughput_suite(scenario: int) -> None:
    print(f"\n===== SCENARIO {scenario} =====")

    if scenario == 3:
        nodeport = get_service_nodeport(GATEWAY_SERVICE)
    else:
        nodeport = get_service_nodeport(SINGLE_MIDDLEBOX_SERVICE)
    host = get_node_host()
    wait_for_nodeport_reachable(
        host,
        nodeport,
        GATEWAY_SERVICE if scenario == 3 else SINGLE_MIDDLEBOX_SERVICE,
        timeout_s=60,
    )
    target = f"{host}:{nodeport}"

    results = []
    raw_rows = []

    for rate_ms in RATES:
        print(f"\n--- Rate {rate_ms}ms ({1000/rate_ms:.2f} Hz) ---")
        planned_requests = compute_requests_for_rate(rate_ms)
        print(
            f"  Finestra {TIME_REQUESTS_SECONDS}s -> richieste pianificate: {planned_requests}"
        )

        if scenario == 3:
            refill_ms = set_refill_period_for_rate(rate_ms)
            scale_middlebox(INITIAL_OPERATORS)
            print(
                f"[SCENARIO3] rate={rate_ms}ms -> refill={refill_ms}ms (45%), "
                f"pool iniziale={INITIAL_OPERATORS}"
            )
            wait_middlebox_pool_ready(INITIAL_OPERATORS)

        test_result = run_throughput_test_scenario_1_or_2(scenario, rate_ms, target)
        latencies = test_result["latencies"]
        dispatch_delays_ms = test_result.get("dispatch_delays_ms", [])
        queue_delays_ms = test_result.get("queue_delays_ms", [])
        total_requests = test_result.get("total_requests", planned_requests)
        status_by_req = test_result.get("status_by_req", ["pending"] * total_requests)
        service_latency_by_req_ns = test_result.get("service_latency_by_req_ns", [None] * total_requests)
        dispatch_delay_by_req_ms = test_result.get("dispatch_delay_by_req_ms", [None] * total_requests)
        queue_delay_by_req_ms = test_result.get("queue_delay_by_req_ms", [None] * total_requests)
        missing = test_result.get("missing", 0)
        missing_timeout = test_result.get("missing_timeout", 0)
        missing_client_error = test_result.get("missing_client_error", 0)
        missing_no_timestamp = test_result.get("missing_no_timestamp", 0)
        missing_other = test_result.get("missing_other", 0)

        stats = compute_stats(latencies)
        dispatch_stats = compute_stats([int(v * 1_000_000) for v in dispatch_delays_ms])
        queue_stats = compute_stats([int(v * 1_000_000) for v in queue_delays_ms])

        total_latencies_ns = []
        for i, lat_ns in enumerate(latencies):
            lat_ms = lat_ns / 1_000_000
            if i < len(queue_delays_ms):
                total_latencies_ns.append(int((lat_ms + queue_delays_ms[i]) * 1_000_000))
            else:
                total_latencies_ns.append(lat_ns)
        total_stats = compute_stats(total_latencies_ns)

        result_row = {
            "scenario": scenario,
            "rate_ms": rate_ms,
            "frequency_hz": round(1000 / rate_ms, 2),
            "num_requests": total_requests,
            "num_requests_ok": stats["count"],
            "missing_requests": missing,
            "missing_timeout": missing_timeout,
            "missing_client_error": missing_client_error,
            "missing_no_timestamp": missing_no_timestamp,
            "missing_other": missing_other,
            "initial_pool": INITIAL_OPERATORS if scenario == 3 else "",
            "refill_threshold": MIN_READY_OPERATORS if scenario == 3 else "",
            "refill_by": SCALE_UP_BY if scenario == 3 else "",
            "refill_period_ms": CURRENT_REFILL_PERIOD_MS if scenario == 3 else "",
            "no_ready_policy": NO_READY_OPERATOR_POLICY if scenario == 3 else "",
            "no_ready_wait_s": NO_READY_OPERATOR_WAIT_SECONDS if scenario == 3 else "",
            "mean_latency_ms": round(stats["mean_ms"], 3),
            "median_latency_ms": round(stats["median_ms"], 3),
            "std_latency_ms": round(stats["std_ms"], 3),
            "min_latency_ms": round(stats["min_ms"], 3),
            "max_latency_ms": round(stats["max_ms"], 3),
            "mean_dispatch_delay_ms": round(dispatch_stats["mean_ms"], 3),
            "median_dispatch_delay_ms": round(dispatch_stats["median_ms"], 3),
            "mean_queue_delay_ms": round(queue_stats["mean_ms"], 3),
            "median_queue_delay_ms": round(queue_stats["median_ms"], 3),
            "mean_total_latency_ms": round(total_stats["mean_ms"], 3),
            "median_total_latency_ms": round(total_stats["median_ms"], 3),
        }
        results.append(result_row)

        for req_idx in range(1, total_requests + 1):
            svc_ns = service_latency_by_req_ns[req_idx - 1] if req_idx - 1 < len(service_latency_by_req_ns) else None
            d_ms = dispatch_delay_by_req_ms[req_idx - 1] if req_idx - 1 < len(dispatch_delay_by_req_ms) else None
            q_ms = queue_delay_by_req_ms[req_idx - 1] if req_idx - 1 < len(queue_delay_by_req_ms) else None
            status = status_by_req[req_idx - 1] if req_idx - 1 < len(status_by_req) else "pending"
            raw_rows.append(
                {
                    "scenario": scenario,
                    "rate_ms": rate_ms,
                    "request_index": req_idx,
                    "status": status,
                    "service_latency_ms": round(svc_ns / 1_000_000, 6) if svc_ns is not None else "",
                    "dispatch_delay_ms": round(d_ms, 6) if d_ms is not None else "",
                    "queue_delay_ms": round(q_ms, 6) if q_ms is not None else "",
                }
            )

        print(f"  Mean service latency: {result_row['mean_latency_ms']} ms")
        print(f"  Mean queue delay: {result_row['mean_queue_delay_ms']} ms")
        print(f"  Mean dispatch delay: {result_row['mean_dispatch_delay_ms']} ms")
        print(f"  Median service latency: {result_row['median_latency_ms']} ms")
        print(f"  Std: {result_row['std_latency_ms']} ms")
        if missing > 0:
            print(
                f"  Missing: {missing} "
                f"(timeout={missing_timeout}, client_error={missing_client_error}, "
                f"no_timestamp={missing_no_timestamp}, other={missing_other})"
            )
        if stats["count"] == 0:
            print("  ATTENZIONE: nessuna latenza valida raccolta; la media 0ms non rappresenta un successo.")
        if len(dispatch_delays_ms) > 0:
            print(f"  (dispatch_delay medio: {dispatch_stats['mean_ms']:.2f}ms - dovrebbe essere < 2-3ms)")

        write_results_csv(results, raw_rows, scenario)

    write_results_csv(results, raw_rows, scenario)


############################################
# MAIN
############################################

def main() -> None:
    refill_stop_event: threading.Event | None = None
    refill_thread: threading.Thread | None = None
    server_process: subprocess.Popen[str] | None = None
    try:
        ensure_directories()
        cleanup_old_output_files()
        ensure_client_binary()
        ensure_kubernetes_cluster_ready()
        ensure_docker_network()
        build_images(include_orchestration=(SCENARIO == 3))
        maybe_load_kind_images(include_orchestration=(SCENARIO == 3))
        server_process = start_server_process()

        if SCENARIO in (1, 2):
            print("\n[SETUP] Kubernetes middlebox singolo scenario 1/2...")
            create_server_bridge()
            create_single_middlebox_pod()
            ensure_nodeport_service(SINGLE_MIDDLEBOX_SERVICE)
            start_middlebox_in_pod(force_regenerate_each_request=(SCENARIO == 1))
        elif SCENARIO == 3:
            print("\n[SETUP] Kubernetes orchestrato scenario 3...")
            cleanup_orchestrate_kubernetes_stack()
            deploy_orchestrate_kubernetes_stack()
            ensure_nodeport_service(GATEWAY_SERVICE)
            refill_stop_event = threading.Event()
            refill_thread = threading.Thread(
                target=run_k8s_refill_loop,
                args=(refill_stop_event,),
                daemon=True,
            )
            refill_thread.start()
            wait_middlebox_pool_ready(INITIAL_OPERATORS)
        else:
            raise ValueError("SCENARIO deve essere 1, 2 o 3")

        try:
            run_throughput_suite(SCENARIO)
        finally:
            if SCENARIO in (1, 2):
                stop_service_in_pod("middlebox")
                delete_single_middlebox_pod()
                run(
                    f"kubectl -n {quote(NAMESPACE)} delete svc {quote(SINGLE_MIDDLEBOX_SERVICE)} --ignore-not-found=true",
                    check=False,
                )
                delete_server_bridge()
            elif SCENARIO == 3:
                if refill_stop_event is not None:
                    refill_stop_event.set()
                if refill_thread is not None:
                    refill_thread.join(timeout=5)
                cleanup_orchestrate_kubernetes_stack()
            stop_server_process(server_process)

        print("\n✓ Test throughput Kubernetes completato")

    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            stop_server_process(server_process)
            if SCENARIO == 3:
                if refill_stop_event is not None:
                    refill_stop_event.set()
                if refill_thread is not None and refill_thread.is_alive():
                    refill_thread.join(timeout=2)
                cleanup_orchestrate_kubernetes_stack()
            if SCENARIO in (1, 2):
                run(
                    f"kubectl -n {quote(NAMESPACE)} delete pod {quote(SINGLE_MIDDLEBOX_POD)} --ignore-not-found=true --wait=false",
                    check=False,
                )
                run(
                    f"kubectl -n {quote(NAMESPACE)} delete svc {quote(SINGLE_MIDDLEBOX_SERVICE)} --ignore-not-found=true",
                    check=False,
                )
                run(
                    f"kubectl -n {quote(NAMESPACE)} delete endpoints {quote(SERVER_K8S_SERVICE)} --ignore-not-found=true",
                    check=False,
                )
                run(
                    f"kubectl -n {quote(NAMESPACE)} delete svc {quote(SERVER_K8S_SERVICE)} --ignore-not-found=true",
                    check=False,
                )
        except Exception:
            pass


if __name__ == "__main__":
    main()
