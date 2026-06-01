from __future__ import annotations

import os
import re
import shlex
import shutil
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path


############################################
# CONFIGURAZIONE
############################################

N = 3
TYPE = "GET"

# Docker network per client e server (stesso di Misure.py)
NETWORK = "tlmsp-net"

# Kubernetes
NAMESPACE = "default"
MIDDLEBOX_POD = "middlebox"
MIDDLEBOX_SVC = "middlebox-svc"
MIDDLEBOX_PORT = 8443
# Percorso del log dentro il pod middlebox
MB_LOG_PATH_IN_POD = "/tmp/Middlebox.log"

SCRIPT_DIR = Path(__file__).resolve().parent


def resolve_project_root(start_dir: Path) -> Path:
    for candidate in [start_dir, *start_dir.parents]:
        if (candidate / "PerformanceMeasuring").exists() and (candidate / "DC/Middlebox").exists():
            return candidate
    raise FileNotFoundError(
        "Impossibile risolvere PROJECT_ROOT: servono le directory PerformanceMeasuring e DC/Middlebox"
    )


PROJECT_ROOT = resolve_project_root(SCRIPT_DIR)

SERVER_DIR = PROJECT_ROOT / "PerformanceMeasuring"
MB_DIR = PROJECT_ROOT / "DC/Middlebox"
GO_PROFESSOR = PROJECT_ROOT / "DC/go/bin/go"
CLIENT_BINARY = MB_DIR / "client"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"

SERVER_CERT_HOST_PATH = SERVER_CERTS_HOST_DIR / "cert.pem"
SERVER_KEY_HOST_PATH = SERVER_CERTS_HOST_DIR / "key.pem"

TOKEN = "token"

# Cartella base storica; ogni run usa una sottocartella timestampata.
BASE_OUTPUT_DIR = PROJECT_ROOT / "MisureKubernetes"


def _run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


BASE_DIR = BASE_OUTPUT_DIR / _run_timestamp()
ANALYSIS_DIR = BASE_DIR / "Analisi"
RUNTIME_DIR = BASE_DIR / "Runtime"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"

CONTAINER_TIMES_LOG = BASE_DIR / "Log_container.txt"

SHARED_VOLUME = "/shared"

CLIENT_CONTAINER = "client"
SERVER_CONTAINER = "server"

CLIENT_IMAGE = "client"
MIDDLEBOX_IMAGE = "middlebox"
SERVER_IMAGE = "server"

STARTUP_TIMEOUT = 60
SETTLE_TIMEOUT = 20
SETTLE_INTERVAL = 0.5
SETTLE_STABLE_FOR = 1.5

_RUN_ENV_CACHE: dict[str, str] | None = None
_NODE_HOST_CACHE: str | None = None
_MIDDLEBOX_NODEPORT_CACHE: int | None = None
_pod_log_offset: int = 0
_SERVER_PROCESS: subprocess.Popen[str] | None = None

TIMESTAMP_PATTERN = re.compile(r"\bt(\d+)\b\s*:\s*[^\n\r]*?=\s*(\d+)")

# Operazione 4 (collegamento diretto client-server) non è presente in Kubernetes
EXPECTED_TIMESTAMPS = {
    "Client": {
        1: {1, 10, 20, 21, 22, 23, 24, 27, 28},
        2: {1, 10, 20, 21, 22, 23, 24, 27, 28},
        3: {1, 10, 20, 21, 22, 23, 24, 27, 28},
    },
    "Middlebox": {
        1: {2, 3, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 37, 38, 39, 40},
        2: {2, 3, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 37, 38, 39, 40},
        3: {2, 3, 37, 38, 39, 40},
    },
    "Server": {
        1: {4, 5, 6, 7, 25, 26},
        2: {4, 5, 6, 7, 25, 26},
        3: set(),
    },
}


############################################
# UTILITA GENERALI
############################################


def ensure_directories() -> None:
    for directory in (BASE_OUTPUT_DIR, BASE_DIR, ANALYSIS_DIR, RUNTIME_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def reset_output_directories() -> None:
    for directory in (ANALYSIS_DIR, RUNTIME_DIR, BASE_DIR / "RawLogs", BASE_DIR / "Logs", BASE_DIR / "Grafici"):
        if directory.exists():
            shutil.rmtree(directory)

    if CONTAINER_TIMES_LOG.exists():
        CONTAINER_TIMES_LOG.unlink()


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


def combine_output(result: subprocess.CompletedProcess[str]) -> str:
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    return stdout + stderr


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
    output = combine_output(result)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Comando fallito con codice {result.returncode}: {command}\n{output}"
        )
    return output


def container_path(host_path: Path) -> str:
    relative_path = host_path.relative_to(BASE_DIR).as_posix()
    return f"{SHARED_VOLUME}/{relative_path}"


def runtime_log_path(component: str) -> Path:
    mapping = {
        "client": CLIENT_RUNTIME_LOG,
        "server": SERVER_RUNTIME_LOG,
    }
    return mapping[component.lower()]


def ensure_network() -> None:
    result = subprocess.run(
        f"docker network inspect {quote(NETWORK)}",
        shell=True,
        capture_output=True,
        text=True,
        env=get_run_env(),
    )
    if result.returncode != 0:
        run(f"docker network create {quote(NETWORK)}")


def ensure_external_certificates() -> None:
    required = [
        SERVER_CERT_HOST_PATH,
        SERVER_KEY_HOST_PATH,
        CLIENT_CA_HOST_PATH,
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        missing_str = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(
            "Certificati esterni mancanti in certs_external. "
            "Genera/ripristina ca.crt, server/cert.pem e server/key.pem.\n"
            f"Mancanti: {missing_str}"
        )


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def file_size(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size


def read_text_from_offset(path: Path, start_offset: int = 0) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        if start_offset > 0:
            handle.seek(start_offset)
        return handle.read().decode("utf-8", errors="replace")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


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


def start_server_process() -> None:
    global _SERVER_PROCESS
    start_idx = file_size(SERVER_RUNTIME_LOG)
    server_log = SERVER_RUNTIME_LOG.open("a", encoding="utf-8")
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
        if marker in read_text_from_offset(SERVER_RUNTIME_LOG, start_idx):
            server_log.close()
            _SERVER_PROCESS = process
            return
        if process.poll() is not None:
            server_log.close()
            content = read_text(SERVER_RUNTIME_LOG)
            raise RuntimeError(f"Il server locale è terminato inaspettatamente. Log:\n{content[-2000:]}")
        time.sleep(0.1)
    server_log.close()
    process.terminate()
    raise TimeoutError(f"Marker non trovato nel log server: {marker}")


def stop_server_process() -> None:
    global _SERVER_PROCESS
    if _SERVER_PROCESS is None:
        return
    if _SERVER_PROCESS.poll() is None:
        _SERVER_PROCESS.terminate()
        try:
            _SERVER_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _SERVER_PROCESS.kill()
            _SERVER_PROCESS.wait(timeout=5)
    _SERVER_PROCESS = None


def detect_baremetal_server_endpoint_ip() -> str:
    def _extract_ipv4(text: str) -> str | None:
        match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        return match.group(0) if match else None

    def _is_loopback(ip: str | None) -> bool:
        return not ip or ip.startswith("127.")

    def _candidate_from_output(text: str) -> str | None:
        ip = _extract_ipv4(text)
        if _is_loopback(ip):
            return None
        return ip

    override = os.environ.get("BAREMETAL_SERVER_ENDPOINT_IP")
    if override:
        override = override.strip()
        if _is_loopback(override):
            raise RuntimeError(
                "BAREMETAL_SERVER_ENDPOINT_IP non può essere loopback (127.x.x.x). "
                "Inserisci un IP host raggiungibile dal pod Kubernetes."
            )
        return override

    context = run("kubectl config current-context", check=False).strip()

    if context.startswith("kind-"):
        probe = run(
            "docker exec kind-control-plane sh -lc \"getent hosts host.docker.internal | awk '{print \\\$1; exit}'\"",
            check=False,
        ).strip()
        extracted = _candidate_from_output(probe)
        if extracted:
            return extracted

        probe = run(
            "docker exec kind-control-plane sh -lc \"ip route | awk '/default/ {print \\\$3; exit}'\"",
            check=False,
        ).strip()
        extracted = _candidate_from_output(probe)
        if extracted:
            return extracted

        probe = run(
            "docker network inspect kind --format '{{(index .IPAM.Config 0).Gateway}}'",
            check=False,
        ).strip()
        extracted = _candidate_from_output(probe)
        if extracted:
            return extracted

    probe = run("hostname -I", check=False).strip()
    for token in probe.split():
        if not _is_loopback(token) and _extract_ipv4(token):
            return token

    probe = run(
        "ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i==\"src\"){print $(i+1); exit}}'",
        check=False,
    ).strip()
    extracted = _candidate_from_output(probe)
    if extracted:
        return extracted

    raise RuntimeError(
        "Impossibile determinare un IP host non-loopback raggiungibile dal pod Kubernetes. "
        "Imposta BAREMETAL_SERVER_ENDPOINT_IP con l'IP corretto del nodo host."
    )


def wait_for_marker(log_path: Path, marker: str, start_index: int = 0) -> None:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if marker in read_text_from_offset(log_path, start_index):
            return
        time.sleep(0.5)
    raise TimeoutError(f"Marker non trovato nel log {log_path}: {marker}")


def wait_until_log_settles(paths: list[Path]) -> None:
    deadline = time.time() + SETTLE_TIMEOUT
    last_sizes: dict[Path, int] = {}
    stable_since: float | None = None

    while time.time() < deadline:
        current_sizes = {
            path: path.stat().st_size if path.exists() else 0
            for path in paths
        }

        if current_sizes == last_sizes:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= SETTLE_STABLE_FOR:
                return
        else:
            stable_since = None

        last_sizes = current_sizes
        time.sleep(SETTLE_INTERVAL)


def parse_ordered_times(text: str) -> list[tuple[int, int]]:
    times: list[tuple[int, int]] = []
    for line in text.splitlines():
        for match in TIMESTAMP_PATTERN.finditer(line):
            times.append((int(match.group(1)), int(match.group(2))))
    return times


def compute_diffs(times: dict[int, int]) -> dict[str, int]:
    diffs: dict[str, int] = {}
    for index in range(2, 20):
        if index in times and (index - 1) in times:
            diffs[f"t{index} - t{index - 1}"] = times[index] - times[index - 1]

    if 11 in times and 8 in times:
        diffs["t11 - t8"] = times[11] - times[8]
    if 9 in times and 19 in times:
        diffs["t9 - t19"] = times[9] - times[19]

    if 1 in times and 10 in times:
        diffs["t10 - t1"] = times[10] - times[1]
    if 37 in times and 38 in times:
        diffs["t38 - t37"] = times[38] - times[37]
    if 38 in times and 39 in times:
        diffs["t39 - t38"] = times[39] - times[38]
    if 39 in times and 40 in times:
        diffs["t40 - t39"] = times[40] - times[39]
    if 40 in times and 10 in times:
        diffs["t10 - t40"] = times[10] - times[40]
    if 25 in times and 26 in times:
        diffs["t26 - t25"] = times[26] - times[25]
    if 20 in times and 21 in times:
        diffs["t21 - t20"] = times[21] - times[20]
    if 27 in times and 28 in times:
        diffs["t28 - t27"] = times[28] - times[27]
    if 22 in times and 23 in times:
        diffs["t23 - t22"] = times[23] - times[22]
    return diffs


def series_label(exp: int, op: int) -> str:
    return f"{exp}_{op}"


def write_experiment_markers(exp: int) -> None:
    marker = f"\n===== ESPERIMENTO {exp} =====\n"
    for path in (CLIENT_RUNTIME_LOG, MIDDLEBOX_RUNTIME_LOG, SERVER_RUNTIME_LOG):
        append_text(path, marker)

    append_text(CONTAINER_TIMES_LOG, marker)


def write_operation_marker(exp: int, op: int) -> None:
    marker = f"--- OPERAZIONE {op} (esperimento {exp}) ---\n"
    append_text(CLIENT_RUNTIME_LOG, marker)
    append_text(MIDDLEBOX_RUNTIME_LOG, marker)
    append_text(SERVER_RUNTIME_LOG, marker)


def append_container_timing(container_name: str, phase: str, timestamp_ns: int) -> None:
    append_text(
        CONTAINER_TIMES_LOG,
        f"{container_name} {phase} = {timestamp_ns} ns\n",
    )


############################################
# KUBERNETES
############################################


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


def get_node_host() -> str:
    global _NODE_HOST_CACHE
    if _NODE_HOST_CACHE is not None:
        return _NODE_HOST_CACHE

    override = os.environ.get("K8S_NODEPORT_HOST", "").strip()
    if override:
        _NODE_HOST_CACHE = override
        return _NODE_HOST_CACHE

    context = run("kubectl config current-context", check=False).strip()

    # Nei cluster kind il NodePort e' esposto sull'IP del container control-plane,
    # non necessariamente su 127.0.0.1.
    if context.startswith("kind-"):
        probe = run(
            "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' kind-control-plane",
            check=False,
        ).strip()
        if probe and re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", probe):
            _NODE_HOST_CACHE = probe
            return _NODE_HOST_CACHE

    # Fallback generico: prova l'InternalIP del primo nodo.
    probe = run(
        "kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type==\"InternalIP\")].address}'",
        check=False,
    ).strip().strip("'")
    if probe and re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", probe):
        _NODE_HOST_CACHE = probe
        return _NODE_HOST_CACHE

    _NODE_HOST_CACHE = "127.0.0.1"
    return _NODE_HOST_CACHE


def ensure_middlebox_nodeport_service() -> None:
    """Crea o aggiorna il NodePort service per il pod middlebox."""
    svc_yaml = (
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        f"  name: {MIDDLEBOX_SVC}\n"
        f"  namespace: {NAMESPACE}\n"
        "spec:\n"
        "  type: NodePort\n"
        "  selector:\n"
        f"    run: {MIDDLEBOX_POD}\n"
        "  ports:\n"
        f"  - port: {MIDDLEBOX_PORT}\n"
        f"    targetPort: {MIDDLEBOX_PORT}\n"
        "    protocol: TCP\n"
    )
    run("\n".join(["cat <<'YAML' | kubectl apply -f -", svc_yaml.strip(), "YAML"]))


def ensure_server_cluster_service_for_baremetal() -> None:
    endpoint_ip = detect_baremetal_server_endpoint_ip()
    manifest = f"""
apiVersion: v1
kind: Service
metadata:
  name: server
  namespace: {NAMESPACE}
spec:
  ports:
    - name: certs
      port: 5000
      protocol: TCP
    - name: app-tls
      port: 8000
      protocol: TCP
---
apiVersion: v1
kind: Endpoints
metadata:
  name: server
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
    run("\n".join(["cat <<'YAML' | kubectl apply -f -", manifest, "YAML"]))


def get_middlebox_nodeport() -> int:
    global _MIDDLEBOX_NODEPORT_CACHE
    if _MIDDLEBOX_NODEPORT_CACHE is not None:
        return _MIDDLEBOX_NODEPORT_CACHE

    deadline = time.time() + 30
    while time.time() < deadline:
        output = run(
            f"kubectl -n {quote(NAMESPACE)} get svc {quote(MIDDLEBOX_SVC)} "
            "-o jsonpath='{.spec.ports[0].nodePort}'",
            check=False,
        ).strip().strip("'")
        if output:
            try:
                _MIDDLEBOX_NODEPORT_CACHE = int(output)
                return _MIDDLEBOX_NODEPORT_CACHE
            except ValueError:
                pass
        time.sleep(1)

    raise RuntimeError(
        f"Impossibile ottenere NodePort del service {MIDDLEBOX_SVC} dopo 30 secondi."
    )


def get_pod_log_content_raw(start_offset: int = 0) -> str:
    """Legge il log del pod, opzionalmente solo dal byte offset richiesto."""
    if start_offset <= 0:
        pod_command = f"cat {quote(MB_LOG_PATH_IN_POD)}"
    else:
        pod_command = f"tail -c +{start_offset + 1} {quote(MB_LOG_PATH_IN_POD)} 2>/dev/null || true"
    return run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(pod_command)}",
        check=False,
    )


def get_pod_log_size() -> int:
    output = run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(f'wc -c < {quote(MB_LOG_PATH_IN_POD)} 2>/dev/null || echo 0')}",
        check=False,
    ).strip()
    try:
        return int(output)
    except ValueError:
        return 0


def wait_for_marker_in_pod(marker: str, start_offset: int = 0) -> None:
    """Aspetta che il marker compaia nel log del pod, a partire dall'offset indicato."""
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if marker in get_pod_log_content_raw(start_offset):
            return
        time.sleep(0.5)
    raise TimeoutError(
        f"Marker non trovato nel log del pod {MIDDLEBOX_POD}: {marker!r}"
    )


def wait_for_any_marker_in_pod(markers: list[str], start_offset: int = 0) -> str:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        chunk = get_pod_log_content_raw(start_offset)
        for marker in markers:
            if marker in chunk:
                return marker
        time.sleep(0.5)
    raise TimeoutError(
        f"Nessuno dei marker trovato nel log del pod {MIDDLEBOX_POD}: {' | '.join(markers)}"
    )


def sync_middlebox_log_increment() -> None:
    """Appende al log host il nuovo contenuto del log del pod (dall'offset corrente)."""
    global _pod_log_offset
    new_content = get_pod_log_content_raw(_pod_log_offset)
    if new_content:
        append_text(MIDDLEBOX_RUNTIME_LOG, new_content)
        _pod_log_offset += len(new_content.encode("utf-8"))


def delete_middlebox_pod() -> None:
    def _pod_exists() -> bool:
        probe = subprocess.run(
            f"kubectl -n {quote(NAMESPACE)} get pod {quote(MIDDLEBOX_POD)} --ignore-not-found",
            shell=True,
            capture_output=True,
            text=True,
            env=get_run_env(),
        )
        if probe.returncode != 0:
            return False
        lines = [line for line in (probe.stdout or "").splitlines() if line.strip()]
        return len(lines) > 1

    run(
        f"kubectl -n {quote(NAMESPACE)} delete pod {quote(MIDDLEBOX_POD)} "
        "--ignore-not-found=true --wait=false",
        check=False,
        timeout=30,
    )

    deadline = time.time() + 120
    while time.time() < deadline:
        if not _pod_exists():
            return
        time.sleep(1)

    # Se il pod resta bloccato in Terminating, forza l'eliminazione.
    run(
        f"kubectl -n {quote(NAMESPACE)} delete pod {quote(MIDDLEBOX_POD)} "
        "--ignore-not-found=true --grace-period=0 --force",
        check=False,
        timeout=30,
    )

    force_deadline = time.time() + 30
    while time.time() < force_deadline:
        if not _pod_exists():
            return
        time.sleep(1)

    raise RuntimeError(
        f"Il pod {MIDDLEBOX_POD} non è stato eliminato correttamente. "
        "Verifica eventuali finalizer o risorse bloccate nel cluster."
    )


def create_middlebox_pod(exp: int) -> None:
    """Crea il pod middlebox e misura il tempo di creazione."""
    global _pod_log_offset
    _pod_log_offset = 0

    start_ns = time.time_ns()
    print(f"!!! CREATE POD {MIDDLEBOX_POD} - START : {start_ns}")
    append_container_timing(MIDDLEBOX_POD, "START", start_ns)

    run(
        f"kubectl -n {quote(NAMESPACE)} run {quote(MIDDLEBOX_POD)} "
        f"--image={quote(MIDDLEBOX_IMAGE)} "
        "--restart=Never "
        "--image-pull-policy=Never "
        "-- tail -f /dev/null"
    )

    run(
        f"kubectl -n {quote(NAMESPACE)} wait --for=condition=Ready "
        f"pod/{quote(MIDDLEBOX_POD)} --timeout=120s"
    )

    end_ns = time.time_ns()
    print(f"!!! CREATE POD {MIDDLEBOX_POD} - END : {end_ns}")
    append_container_timing(MIDDLEBOX_POD, "END", end_ns)

    pod_creation_time_ns = end_ns - start_ns
    append_text(
        MIDDLEBOX_RUNTIME_LOG,
        f"[MB] Pod creation time = {pod_creation_time_ns} ns\n",
    )

    # Inizializza il file di log dentro il pod
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -c 'mkdir -p /tmp && touch {quote(MB_LOG_PATH_IN_POD)}'",
        check=False,
    )


def stop_service_in_pod(process_pattern: str) -> None:
    kill_command = (
        f"pkill -INT -f {shlex.quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -TERM -f {shlex.quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -KILL -f {shlex.quote(process_pattern)} 2>/dev/null || true"
    )
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(kill_command)}",
        check=False,
    )


def start_middlebox_in_pod() -> None:
    """Avvia il processo middlebox dentro il pod e aspetta che sia pronto."""
    current_size = get_pod_log_size()
    inner_cmd = f"cd /app && ./middlebox >> {MB_LOG_PATH_IN_POD} 2>&1 &"
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(inner_cmd)}"
    )
    wait_for_any_marker_in_pod(
        [
            "[MB] Middlebox TLS server listening on :8443",
            "[MB] Middlebox listening on :8443",
        ],
        start_offset=current_size,
    )

    startup_slice_in_pod = get_pod_log_content_raw(current_size)
    if "address already in use" in startup_slice_in_pod:
        raise RuntimeError(
            f"Avvio middlebox nel pod fallito: porta già in uso.\n{startup_slice_in_pod}"
        )


############################################
# DOCKER (client e server)
############################################


def cleanup_docker_containers() -> None:
    return


def build_images() -> None:
    print(f"!!! BUILD MIDDLEBOX CONTAINER - START : {time.time_ns()}")
    dockerfile_rel = MB_DIR.relative_to(PROJECT_ROOT) / "Dockerfile.middlebox"
    run(f"docker build -f {quote(str(dockerfile_rel))} -t middlebox .", cwd=PROJECT_ROOT)
    print(f"!!! BUILD MIDDLEBOX CONTAINER - END : {time.time_ns()}")


def start_idle_docker_container(name: str, image: str) -> None:
    start_ns = time.time_ns()
    append_container_timing(name, "START", start_ns)

    run_args = [
        "docker run -d",
        f"--name {quote(name)}",
        f"--network {quote(NETWORK)}",
        f"-v {quote(str(BASE_DIR))}:{quote(SHARED_VOLUME)}",
    ]

    if name == SERVER_CONTAINER:
        run_args.append(f"-v {quote(str(SERVER_CERTS_HOST_DIR))}:/certs")
    elif name == CLIENT_CONTAINER:
        run_args.append(f"-v {quote(str(CLIENT_CA_HOST_PATH))}:/certs/ca.crt:ro")

    run_args.extend([quote(image), "tail -f /dev/null"])
    run(" ".join(run_args))

    end_ns = time.time_ns()
    append_container_timing(name, "END", end_ns)


def start_docker_service(
    *,
    container: str,
    component: str,
    start_command: str,
    marker: str,
) -> None:
    log_path = runtime_log_path(component)
    start_index = file_size(log_path)

    container_log_path = container_path(log_path)
    inner_command = f"cd /app && {start_command} >> {quote(container_log_path)} 2>&1"

    run(f"docker exec -d {quote(container)} sh -lc {quote(inner_command)}")
    wait_for_marker(log_path, marker, start_index)

    startup_slice = read_text_from_offset(log_path, start_index)
    if "address already in use" in startup_slice:
        raise RuntimeError(
            f"Avvio servizio fallito per {component}: porta già in uso.\n{startup_slice}"
        )


def stop_docker_service(container: str, process_pattern: str) -> None:
    kill_command = (
        f"pkill -INT -f {quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -TERM -f {quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -KILL -f {quote(process_pattern)} 2>/dev/null || true"
    )
    run(f"docker exec {quote(container)} sh -lc {quote(kill_command)}", check=False)


def start_server() -> None:
    start_server_process()


def start_middlebox() -> None:
    start_middlebox_in_pod()


def create_docker_containers() -> None:
    return


############################################
# RACCOLTA LOG
############################################


def client_request(target: str) -> str:
    url = f"https://{target}/function/init"

    cmd = [
        str(CLIENT_BINARY),
        "-ca",
        str(CLIENT_CA_HOST_PATH),
        "-servername",
        "server",
        "-H",
        f"Authorization : Bearer {TOKEN}",
    ]
    if TYPE.upper() == "POST":
        cmd.extend(["-data", "{}"])
    elif TYPE.upper() != "GET":
        raise ValueError("TYPE deve essere GET oppure POST")
    cmd.append(url)
    completed = subprocess.run(
        cmd,
        cwd=str(MB_DIR),
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    append_text(CLIENT_RUNTIME_LOG, output)

    has_call = "Calling:" in output
    has_ok = '"status": "ok"' in output
    has_error = (
        "Error during request" in output
        or "connection refused" in output
        or "panic:" in output
    )
    if not has_call or not has_ok or has_error:
        raise RuntimeError(f"Richiesta client fallita verso {url}\n{output}")

    return output


def wait_logs_after_operation() -> None:
    # Aspetta che client e server terminino di scrivere
    wait_until_log_settles([SERVER_RUNTIME_LOG, CLIENT_RUNTIME_LOG])
    # Piccola attesa aggiuntiva per il log dentro il pod
    time.sleep(0.5)
    # Sincronizza il log del pod verso l'host
    sync_middlebox_log_increment()


############################################
# ANALISI DA LOG AGGREGATI
############################################


def split_experiment_sections(text: str) -> dict[int, str]:
    sections: dict[int, list[str]] = {}
    current_exp: int | None = None

    for line in text.splitlines():
        marker_match = re.match(r"^===== ESPERIMENTO (\d+) =====$", line.strip())
        if marker_match:
            current_exp = int(marker_match.group(1))
            sections.setdefault(current_exp, [])
            continue

        if current_exp is not None:
            sections[current_exp].append(line)

    return {exp: "\n".join(lines) for exp, lines in sections.items()}


def split_operation_sections(text: str) -> dict[int, str]:
    sections: dict[int, list[str]] = {}
    current_op: int | None = None

    for line in text.splitlines():
        marker_match = re.match(r"^--- OPERAZIONE (\d+) \(esperimento \d+\) ---$", line.strip())
        if marker_match:
            current_op = int(marker_match.group(1))
            sections.setdefault(current_op, [])
            continue

        if current_op is not None:
            sections[current_op].append(line)

    return {op: "\n".join(lines) for op, lines in sections.items()}


def collect_component_times_by_experiment(component: str, path: Path) -> dict[int, list[tuple[int, int]]]:
    component_sections = split_experiment_sections(read_text(path))
    allowed = set().union(*EXPECTED_TIMESTAMPS[component].values())

    parsed: dict[int, list[tuple[int, int]]] = {}
    for exp, section in component_sections.items():
        ordered = [
            (ts, value)
            for ts, value in parse_ordered_times(section)
            if ts in allowed
        ]
        parsed[exp] = ordered

    return parsed


def latest_times_per_operation(text: str, allowed: set[int]) -> dict[int, dict[int, int]]:
    # Solo operazioni 1-3 per Kubernetes
    operations: dict[int, dict[int, int]] = {1: {}, 2: {}, 3: {}}

    for op, op_text in split_operation_sections(text).items():
        times: dict[int, int] = {}
        for ts, value in parse_ordered_times(op_text):
            if ts in allowed:
                times[ts] = value
        if op in operations:
            operations[op] = times

    return operations


def reconstruct_operations_from_runtime() -> dict[int, dict[int, dict[int, int]]]:
    client_sections = split_experiment_sections(read_text(CLIENT_RUNTIME_LOG))
    middlebox_sections = split_experiment_sections(read_text(MIDDLEBOX_RUNTIME_LOG))
    server_sections = split_experiment_sections(read_text(SERVER_RUNTIME_LOG))

    all_exp = sorted(set(client_sections) | set(middlebox_sections) | set(server_sections))
    # Solo operazioni 1-3 per Kubernetes
    reconstructed: dict[int, dict[int, dict[int, int]]] = {
        exp: {1: {}, 2: {}, 3: {}}
        for exp in all_exp
    }

    client_allowed = set().union(*EXPECTED_TIMESTAMPS["Client"].values())
    middlebox_allowed = set().union(*EXPECTED_TIMESTAMPS["Middlebox"].values())
    server_allowed = set().union(*EXPECTED_TIMESTAMPS["Server"].values())

    for exp in all_exp:
        client_ops = latest_times_per_operation(client_sections.get(exp, ""), client_allowed)
        middlebox_ops = latest_times_per_operation(middlebox_sections.get(exp, ""), middlebox_allowed)
        server_ops = latest_times_per_operation(server_sections.get(exp, ""), server_allowed)

        for op in range(1, 4):
            merged = {}
            merged.update(client_ops.get(op, {}))
            merged.update(middlebox_ops.get(op, {}))
            merged.update(server_ops.get(op, {}))
            reconstructed[exp][op] = merged

    return reconstructed


def analyze_logs() -> None:
    results_path = ANALYSIS_DIR / "Risultati.txt"
    graphs_path = ANALYSIS_DIR / "Grafici.txt"

    operations = reconstruct_operations_from_runtime()

    # Solo operazioni 1-3
    averages: dict[int, dict[str, defaultdict[int | str, list[int]]]] = {
        op: {
            "times": defaultdict(list),
            "diffs": defaultdict(list),
        }
        for op in range(1, 4)
    }

    graph_points: dict[int, dict[str, list[tuple[int, int]]]] = {
        op: {"t1": [], "t10": [], "t10 - t1": []}
        for op in range(1, 4)
    }

    with results_path.open("w", encoding="utf-8") as results_file:
        for exp in sorted(operations):
            for op in range(1, 4):
                merged_times = operations[exp][op]
                diffs = compute_diffs(merged_times)

                results_file.write(f"OPERAZIONE {exp}_{op}:\n")

                for key in sorted(merged_times):
                    value = merged_times[key]
                    results_file.write(f"t{key} = {value}\n")
                    averages[op]["times"][key].append(value)

                for label, value in diffs.items():
                    results_file.write(f"{label} = {value} ns\n")
                    averages[op]["diffs"][label].append(value)

                if 1 in merged_times:
                    graph_points[op]["t1"].append((exp, merged_times[1]))
                if 10 in merged_times:
                    graph_points[op]["t10"].append((exp, merged_times[10]))
                if "t10 - t1" in diffs:
                    graph_points[op]["t10 - t1"].append((exp, diffs["t10 - t1"]))

                results_file.write("--------------------\n")

        for op in range(1, 4):
            results_file.write(f"AVERAGE operazione_N_{op}:\n")

            for key in sorted(averages[op]["times"]):
                avg_value = round(statistics.fmean(averages[op]["times"][key]))
                results_file.write(f"t{key} average = {avg_value} ns\n")

            diff_order = [
                "t2 - t1",
                "t28 - t27",
                "t3 - t2",
                "t4 - t3",
                "t38 - t37",
                "t39 - t38",
                "t40 - t39",
                "t26 - t25",
                "t5 - t4",
                "t6 - t5",
                "t7 - t6",
                "t8 - t7",
                "t11 - t8",
                "t12 - t11",
                "t13 - t12",
                "t14 - t13",
                "t15 - t14",
                "t16 - t15",
                "t17 - t16",
                "t18 - t17",
                "t19 - t18",
                "t9 - t19",
                "t9 - t8",
                "t10 - t9",
                "t10 - t40",
                "t10 - t1",
                "t21 - t20",
                "t23 - t22",
            ]
            for label in diff_order:
                values = averages[op]["diffs"].get(label, [])
                if values:
                    avg_value = round(statistics.fmean(values))
                    results_file.write(f"{label} average = {avg_value} ns\n")

            results_file.write("--------------------\n")

    with graphs_path.open("w", encoding="utf-8") as graphs_file:
        for metric in ("t1", "t10", "t10 - t1"):
            for op in range(1, 4):
                graphs_file.write(f"SERIE {metric} operazione_N_{op}:\n")
                for exp, value in graph_points[op][metric]:
                    graphs_file.write(f"{series_label(exp, op)} = {value}\n")
                graphs_file.write("--------------------\n")


############################################
# ESPERIMENTI
############################################


def reset_middlebox_delegation() -> None:
    """Mantiene il middlebox attivo, svuota cache deleghe e rimuove i file delega."""
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote('pkill -USR1 -f middlebox 2>/dev/null || true')}",
        check=False,
    )

    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote('rm -f /certs/dc.cred /certs/dckey.pem')}",
        check=True,
    )

    verify_cmd = (
        "if [ -f /certs/dc.cred ] || [ -f /certs/dckey.pem ]; then "
        "echo '[MB] Deleghe non eliminate'; exit 1; "
        "fi"
    )
    run(
        f"kubectl -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(verify_cmd)}",
        check=True,
    )


def stop_experiment_services() -> None:
    stop_service_in_pod("middlebox")
    stop_server_process()
    time.sleep(1)


def run_single_experiment(exp: int) -> None:
    node_host = get_node_host()
    nodeport = get_middlebox_nodeport()
    middlebox_addr = f"{node_host}:{nodeport}"

    print(f"\n===== ESPERIMENTO {exp} =====\n")
    write_experiment_markers(exp)

    cleanup_docker_containers()
    delete_middlebox_pod()

    try:
        # Crea containers Docker (server, client)
        create_docker_containers()
        # Crea pod Kubernetes middlebox (misura tempo creazione)
        create_middlebox_pod(exp)

        start_server()
        start_middlebox()

        print("OPERAZIONE 1")
        write_operation_marker(exp, 1)
        client_request(middlebox_addr)
        wait_logs_after_operation()

        print("OPERAZIONE 2")
        write_operation_marker(exp, 2)
        reset_middlebox_delegation()
        client_request(middlebox_addr)
        wait_logs_after_operation()

        print("OPERAZIONE 3")
        write_operation_marker(exp, 3)
        client_request(middlebox_addr)
        wait_logs_after_operation()

    finally:
        stop_experiment_services()
        cleanup_docker_containers()
        delete_middlebox_pod()


def run_experiments() -> None:
    global _MIDDLEBOX_NODEPORT_CACHE, _NODE_HOST_CACHE

    reset_output_directories()
    ensure_directories()
    ensure_kubernetes_cluster_ready()
    ensure_network()
    ensure_external_certificates()
    ensure_client_binary()

    # Crea il NodePort service (persiste tra gli esperimenti, solo il pod viene ricreato)
    _MIDDLEBOX_NODEPORT_CACHE = None
    _NODE_HOST_CACHE = None
    cleanup_docker_containers()
    delete_middlebox_pod()

    ensure_middlebox_nodeport_service()
    ensure_server_cluster_service_for_baremetal()

    build_images()

    # Carica l'immagine nel cluster kind se necessario
    _maybe_load_kind_image()

    for exp in range(1, N + 1):
        run_single_experiment(exp)


def _maybe_load_kind_image() -> None:
    """Carica le immagini Docker nel cluster kind (se il contesto è kind-*)."""
    context = run("kubectl config current-context", check=False).strip()
    if not context.startswith("kind-"):
        return

    import shutil as _shutil
    if _shutil.which("kind") is None:
        print("AVVISO: context kind rilevato ma comando 'kind' non trovato. "
              "Assicurati che l'immagine middlebox sia già caricata nel cluster.")
        return

    kind_cluster = context.removeprefix("kind-")
    print(f"Context kind rilevato: carico immagine {MIDDLEBOX_IMAGE} nel cluster {kind_cluster}")
    run(f"kind load docker-image {quote(MIDDLEBOX_IMAGE)} --name {quote(kind_cluster)}")


def emergency_cleanup() -> None:
    """Pulizia aggressiva finale."""
    critical_ports = [5000, 8000, 8443]
    for port in critical_ports:
        try:
            subprocess.run(f"fuser -k {port}/tcp 2>/dev/null", shell=True, check=False)
        except Exception:
            pass

    stop_server_process()

    try:
        run(
            f"kubectl -n {quote(NAMESPACE)} delete pod {quote(MIDDLEBOX_POD)} "
            "--ignore-not-found=true --wait=false",
            check=False,
        )
    except Exception:
        pass

    try:
        run(
            f"kubectl -n {quote(NAMESPACE)} delete svc {quote(MIDDLEBOX_SVC)} "
            "--ignore-not-found=true --wait=false",
            check=False,
        )
    except Exception:
        pass


def verify_cleanup() -> bool:
    critical_ports = [5000, 8000, 8443]
    all_clear = True

    for port in critical_ports:
        proc = subprocess.run(
            f"ss -ltnp | grep :{port}",
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.stdout.strip():
            print(f"ATTENZIONE: Porta {port} ancora in ascolto: {proc.stdout.strip()}")
            all_clear = False
        else:
            print(f"✓ Porta {port} libera")

    return all_clear


def main() -> None:
    try:
        run_experiments()
        analyze_logs()
        print("\nEsperimenti Kubernetes completati")
    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            stop_server_process()
            emergency_cleanup()
            verify_cleanup()
        except Exception as exc:
            print(f"Errore durante emergency_cleanup: {exc}")


if __name__ == "__main__":
    main()
