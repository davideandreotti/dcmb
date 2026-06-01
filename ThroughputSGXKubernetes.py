#!/usr/bin/env python3
from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shlex
import shutil
import socket
import statistics
import subprocess
import threading
import time
from pathlib import Path

# Array di rate (ms tra richieste)
RATES = [10, 8, 6, 4, 2, 1]
# Finestra temporale (secondi) entro cui inviare tutte le richieste possibili per ogni rate
TIME_REQUESTS_SECONDS = 15

# Scenario: 1 = middlebox SGX forza reset/re-auth per richiesta, 2 = delega/cache mantenuta
SCENARIO = 1

# Kubernetes
NAMESPACE = "default"
SINGLE_MIDDLEBOX_POD = "middlebox-sgx-throughput"
SINGLE_MIDDLEBOX_APP_LABEL = "middlebox-sgx-throughput"
SINGLE_MIDDLEBOX_SERVICE = "middlebox-sgx-throughput-svc"
SINGLE_MIDDLEBOX_PORT = 8443
MIDDLEBOX_SGX_IMAGE = "middleboxsgxshield:latest"

SERVER_K8S_SERVICE = "server"  # deve corrispondere a http://server:5000 hardcoded nel middlebox

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
MIDDLEBOX_SGX_BINARY = MB_DIR / "middleboxsgx"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"

TOKEN = "token"

BASE_DIR = PROJECT_ROOT / "ThroughputSGXKubernetes"
RESULTS_DIR = BASE_DIR / "Risultati"
RUNTIME_DIR = BASE_DIR / "Runtime"
GRAFICI_DIR = BASE_DIR / "Grafici"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"

STARTUP_TIMEOUT = 120
CLIENT_REQUEST_TIMEOUT_SECONDS = 120
MAX_CLIENT_WORKERS = 128
POD_READY_TIMEOUT_SECONDS = 240
POD_READY_POLL_SECONDS = 1.0

# Baseline service time (t10-t1 medio) usata per stimare la coda lato middlebox sotto stress.
BASELINE_SERVICE_TIME_MS = {
    1: 13.10,
    2: 4.90,
}

_RUN_ENV_CACHE: dict[str, str] | None = None

# Se True, in caso di mismatch hash rigenera automaticamente manifest.sgx/sig.
AUTO_REPAIR_SGX_ARTIFACTS = True


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


def write_results_csv(results: list[dict], raw_rows: list[dict]) -> None:
    csv_path = RESULTS_DIR / f"Throughput_Scenario_{SCENARIO}.csv"
    if results:
        with open(csv_path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"\n✓ Risultati salvati: {csv_path}")

    raw_csv_path = RESULTS_DIR / f"Throughput_Scenario_{SCENARIO}_raw.csv"
    if raw_rows:
        with open(raw_csv_path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=raw_rows[0].keys())
            writer.writeheader()
            writer.writerows(raw_rows)
        print(f"✓ Latenze raw salvate: {raw_csv_path}")


def extract_ip_address(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line)
        if match:
            return match.group(0)
    return None


def ensure_client_binary() -> None:
    source = MB_DIR / "client.go"
    if CLIENT_BINARY.exists() and CLIENT_BINARY.stat().st_mtime >= source.stat().st_mtime:
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


def ensure_middlebox_sgx_binary() -> None:
    """
    In modalita SGX il binary deve restare allineato a manifest/sig.
    Ricompilazioni automatiche rischiano hash mismatch all'avvio enclave.
    """
    if MIDDLEBOX_SGX_BINARY.exists():
        return
    raise FileNotFoundError(
        "Binary SGX non trovato: "
        f"{MIDDLEBOX_SGX_BINARY}. "
        "Compila/firma il middlebox SGX prima di eseguire questo script."
    )


def extract_trusted_middlebox_hash(manifest_path: Path) -> str | None:
    text = read_text(manifest_path)
    if not text:
        return None
    marker = 'uri = "file:middleboxsgx"'
    marker_pos = text.find(marker)
    if marker_pos == -1:
        return None
    tail = text[marker_pos:]
    match = re.search(r'sha256\s*=\s*"([0-9a-fA-F]{64})"', tail)
    if not match:
        return None
    return match.group(1).lower()


def sha256_file(path: Path) -> str:
    result = subprocess.run(
        ["sha256sum", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0 or not output:
        raise RuntimeError(f"Impossibile calcolare sha256 per {path}: {output}")
    return output.split()[0].strip().lower()


def rewrite_middlebox_hash_in_source_manifest(source_manifest: Path, binary_hash: str) -> None:
    text = read_text(source_manifest)
    if not text:
        raise RuntimeError(f"Manifest SGX sorgente vuoto/non leggibile: {source_manifest}")

    marker = 'uri = "file:middleboxsgx"'
    marker_pos = text.find(marker)
    if marker_pos == -1:
        raise RuntimeError(
            f"Voce trusted file middleboxsgx non trovata in {source_manifest}"
        )

    tail = text[marker_pos:]
    hash_match = re.search(r'sha256\s*=\s*"([0-9a-fA-F]{64})"', tail)
    if not hash_match:
        raise RuntimeError(
            f"Hash trusted non trovato dopo uri middleboxsgx in {source_manifest}"
        )

    start = marker_pos + hash_match.start(1)
    end = marker_pos + hash_match.end(1)
    updated = text[:start] + binary_hash + text[end:]
    source_manifest.write_text(updated, encoding="utf-8")


def regenerate_sgx_artifacts(binary_hash: str) -> None:
    source_manifest = MB_DIR / "middleboxsgx.manifest"
    key_path = MB_DIR / "enclave-key.pem"
    output_manifest = MB_DIR / "middleboxsgx.manifest.sgx"

    if not source_manifest.exists():
        raise FileNotFoundError(f"Manifest SGX sorgente non trovato: {source_manifest}")
    if not key_path.exists():
        raise FileNotFoundError(f"Chiave enclave non trovata: {key_path}")
    if shutil.which("gramine-sgx-sign") is None:
        raise RuntimeError("Comando gramine-sgx-sign non disponibile nel PATH")

    rewrite_middlebox_hash_in_source_manifest(source_manifest, binary_hash)

    result = subprocess.run(
        [
            "gramine-sgx-sign",
            "--manifest",
            str(source_manifest),
            "--output",
            str(output_manifest),
            "--key",
            str(key_path),
        ],
        cwd=str(MB_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        raise RuntimeError(f"Rigenerazione artefatti SGX fallita:\n{output}")


def ensure_sgx_artifacts_consistency() -> None:
    manifest_path = MB_DIR / "middleboxsgx.manifest.sgx"
    sig_path = MB_DIR / "middleboxsgx.sig"
    missing = [str(p) for p in (MIDDLEBOX_SGX_BINARY, manifest_path, sig_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Artefatti SGX mancanti:\n- " + "\n- ".join(missing)
        )

    expected_hash = extract_trusted_middlebox_hash(manifest_path)
    if expected_hash is None:
        raise RuntimeError(
            "Impossibile leggere hash trusted di middleboxsgx da "
            f"{manifest_path}."
        )

    actual_hash = sha256_file(MIDDLEBOX_SGX_BINARY)
    if actual_hash != expected_hash:
        if AUTO_REPAIR_SGX_ARTIFACTS:
            regenerate_sgx_artifacts(actual_hash)
            expected_hash = extract_trusted_middlebox_hash(manifest_path)
            if expected_hash == actual_hash:
                return

        raise RuntimeError(
            "Mismatch artefatti SGX: hash middleboxsgx diverso dal manifest.\n"
            f"- binary:   {MIDDLEBOX_SGX_BINARY}\n"
            f"  sha256={actual_hash}\n"
            f"- manifest: {manifest_path}\n"
            f"  sha256={expected_hash}\n"
            "Rigenera manifest+sig coerenti col binary oppure ripristina la tripletta compatibile "
            "(middleboxsgx, middleboxsgx.manifest.sgx, middleboxsgx.sig)."
        )


def build_sgx_image() -> None:
    print(f"!!! BUILD SGX IMAGE - START : {time.time_ns()}")
    run("docker build -f middleboxsgxshield.Dockerfile -t middleboxsgxshield:latest .", cwd=MB_DIR)
    print(f"!!! BUILD SGX IMAGE - END : {time.time_ns()}")


def cleanup_stale_server_processes() -> None:
    # Elimina eventuali server locali rimasti da esecuzioni precedenti.
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

    # Trunca il log ad ogni avvio per evitare marker residui da run precedenti.
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
                f"Il server locale e' terminato inaspettatamente. Log:\n{content[-2000:]}{diagnostics}"
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


def maybe_load_kind_images() -> None:
    context = run("kubectl config current-context", check=False).strip()
    if not context.startswith("kind-"):
        return
    kind_cluster_name = context.removeprefix("kind-") or "kind"
    run(f"kind load docker-image {quote(MIDDLEBOX_SGX_IMAGE)} --name {quote(kind_cluster_name)}", check=True)


############################################
# KUBERNETES - MIDDLEBOX SGX SINGOLO
############################################

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


def delete_single_middlebox_sgx_pod() -> None:
    run(
        f"kubectl -n {quote(NAMESPACE)} delete svc {quote(SINGLE_MIDDLEBOX_SERVICE)} --ignore-not-found=true",
        check=False,
    )
    run(
        f"kubectl -n {quote(NAMESPACE)} delete pod {quote(SINGLE_MIDDLEBOX_POD)} --ignore-not-found=true --wait=true",
        check=False,
    )


def create_single_middlebox_sgx_pod(force_regenerate_each_request: bool = False) -> None:
    delete_single_middlebox_sgx_pod()
    force_regen_value = "1" if force_regenerate_each_request else "0"
    manifest = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {SINGLE_MIDDLEBOX_POD}
  namespace: {NAMESPACE}
  labels:
    app: {SINGLE_MIDDLEBOX_APP_LABEL}
spec:
  restartPolicy: Never
  containers:
    - name: middlebox-sgx
      image: {MIDDLEBOX_SGX_IMAGE}
      imagePullPolicy: IfNotPresent
      env:
        - name: MB_FORCE_REGENERATE_DC_EACH_REQUEST
          value: "{force_regen_value}"
      securityContext:
        privileged: true
        runAsUser: 0
        allowPrivilegeEscalation: true
      ports:
        - containerPort: {SINGLE_MIDDLEBOX_PORT}
          name: https-mb
          protocol: TCP
      volumeMounts:
        - name: dev-sgx-enclave
          mountPath: /dev/sgx_enclave
        - name: dev-sgx-provision
          mountPath: /dev/sgx_provision
        - name: certs-dir
          mountPath: /certs
      startupProbe:
        tcpSocket:
          port: {SINGLE_MIDDLEBOX_PORT}
        failureThreshold: 240
        periodSeconds: 1
      readinessProbe:
        tcpSocket:
          port: {SINGLE_MIDDLEBOX_PORT}
        initialDelaySeconds: 1
        periodSeconds: 2
        timeoutSeconds: 1
        failureThreshold: 60
      livenessProbe:
        tcpSocket:
          port: {SINGLE_MIDDLEBOX_PORT}
        initialDelaySeconds: 20
        periodSeconds: 10
        timeoutSeconds: 1
        failureThreshold: 6
  volumes:
    - name: dev-sgx-enclave
      hostPath:
        path: /dev/sgx_enclave
        type: CharDevice
    - name: dev-sgx-provision
      hostPath:
        path: /dev/sgx_provision
        type: CharDevice
    - name: certs-dir
      emptyDir: {{}}
---
apiVersion: v1
kind: Service
metadata:
  name: {SINGLE_MIDDLEBOX_SERVICE}
  namespace: {NAMESPACE}
spec:
  type: NodePort
  selector:
    app: {SINGLE_MIDDLEBOX_APP_LABEL}
  ports:
    - name: https
      protocol: TCP
      port: {SINGLE_MIDDLEBOX_PORT}
      targetPort: {SINGLE_MIDDLEBOX_PORT}
""".strip()
    run("\n".join(["cat <<'YAML' | kubectl apply -f -", manifest, "YAML"]))
    wait_for_pod_ready_or_fail(SINGLE_MIDDLEBOX_POD, timeout_s=POD_READY_TIMEOUT_SECONDS)


def wait_for_pod_ready_or_fail(pod_name: str, timeout_s: int) -> None:
    deadline = time.time() + timeout_s
    last_phase = ""
    while time.time() < deadline:
        pod_json = run(
            f"kubectl -n {quote(NAMESPACE)} get pod {quote(pod_name)} -o json",
            check=False,
        )
        if not pod_json.strip():
            time.sleep(POD_READY_POLL_SECONDS)
            continue
        try:
            pod_obj = json.loads(pod_json)
        except json.JSONDecodeError:
            time.sleep(POD_READY_POLL_SECONDS)
            continue

        status = pod_obj.get("status", {}) if isinstance(pod_obj, dict) else {}
        phase = str(status.get("phase", ""))
        last_phase = phase or last_phase

        conditions = status.get("conditions", []) or []
        ready = any(
            (cond.get("type") == "Ready" and cond.get("status") == "True")
            for cond in conditions
            if isinstance(cond, dict)
        )
        if ready:
            return

        container_statuses = status.get("containerStatuses", []) or []
        if container_statuses and isinstance(container_statuses[0], dict):
            state = container_statuses[0].get("state", {}) or {}
            terminated = state.get("terminated")
            waiting = state.get("waiting")
            if terminated:
                reason = terminated.get("reason", "")
                exit_code = terminated.get("exitCode", "")
                logs = run(
                    f"kubectl -n {quote(NAMESPACE)} logs {quote(pod_name)} --tail=250",
                    check=False,
                )
                describe = run(
                    f"kubectl -n {quote(NAMESPACE)} describe pod {quote(pod_name)}",
                    check=False,
                )
                raise RuntimeError(
                    f"Pod SGX terminato prima di Ready (phase={phase}, reason={reason}, exit={exit_code}).\n"
                    f"Log recenti:\n{logs[-3000:]}\n\nDescribe (tail):\n{describe[-4000:]}"
                )
            if waiting:
                wait_reason = str(waiting.get("reason", ""))
                if wait_reason in {"ErrImagePull", "ImagePullBackOff", "CreateContainerConfigError", "CreateContainerError"}:
                    describe = run(
                        f"kubectl -n {quote(NAMESPACE)} describe pod {quote(pod_name)}",
                        check=False,
                    )
                    raise RuntimeError(
                        f"Pod SGX bloccato in waiting={wait_reason} prima di Ready.\n{describe[-4000:]}"
                    )

        if phase in {"Failed", "Succeeded"}:
            logs = run(
                f"kubectl -n {quote(NAMESPACE)} logs {quote(pod_name)} --tail=250",
                check=False,
            )
            describe = run(
                f"kubectl -n {quote(NAMESPACE)} describe pod {quote(pod_name)}",
                check=False,
            )
            raise RuntimeError(
                f"Pod SGX non Ready (phase={phase}).\n"
                f"Log recenti:\n{logs[-3000:]}\n\nDescribe (tail):\n{describe[-4000:]}"
            )

        time.sleep(POD_READY_POLL_SECONDS)

    logs = run(
        f"kubectl -n {quote(NAMESPACE)} logs {quote(pod_name)} --tail=250",
        check=False,
    )
    describe = run(
        f"kubectl -n {quote(NAMESPACE)} describe pod {quote(pod_name)}",
        check=False,
    )
    raise TimeoutError(
        f"Timeout attesa Ready pod/{pod_name} ({timeout_s}s, last_phase={last_phase}).\n"
        f"Log recenti:\n{logs[-3000:]}\n\nDescribe (tail):\n{describe[-4000:]}"
    )


def wait_for_marker_in_pod_logs(marker: str) -> None:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        content = run(
            f"kubectl -n {quote(NAMESPACE)} logs {quote(SINGLE_MIDDLEBOX_POD)} --tail=200",
            check=False,
        )
        append_text(MIDDLEBOX_RUNTIME_LOG, content)
        if marker in content:
            return
        pod_json = run(
            f"kubectl -n {quote(NAMESPACE)} get pod {quote(SINGLE_MIDDLEBOX_POD)} -o json",
            check=False,
        )
        try:
            phase = (json.loads(pod_json) or {}).get("status", {}).get("phase", "")
        except json.JSONDecodeError:
            phase = ""
        if phase in {"Failed", "Succeeded"}:
            raise RuntimeError(f"Pod SGX terminato prematuramente con phase={phase}. Log:\n{content[-2000:]}")
        time.sleep(1)
    raise TimeoutError(f"Marker non trovato nei log del pod {SINGLE_MIDDLEBOX_POD}: {marker}")


def get_service_nodeport(service_name: str, timeout_s: int = 60) -> int:
    started = time.time()
    while True:
        output = run(
            f"kubectl -n {quote(NAMESPACE)} get svc {quote(service_name)} -o jsonpath='{{.spec.ports[0].nodePort}}'",
            check=False,
        ).strip().strip("'")
        if output:
            return int(output)
        if time.time() - started > timeout_s:
            raise RuntimeError(f"Impossibile ottenere NodePort del service {service_name}")
        time.sleep(0.5)


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

        # Su Linux, NodePort in kind risponde sull'IP del container control-plane,
        # non necessariamente su 127.0.0.1.
        probe = run(
            "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' kind-control-plane",
            check=False,
        ).strip()
        ip = extract_ip_address(probe)
        if ip:
            return ip

    return "127.0.0.1"


def wait_for_nodeport_reachable(host: str, port: int, timeout_s: int = 60) -> None:
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
        f"kubectl -n {quote(NAMESPACE)} get svc {quote(SINGLE_MIDDLEBOX_SERVICE)} -o wide",
        check=False,
    )
    ep = run(
        f"kubectl -n {quote(NAMESPACE)} get endpoints {quote(SINGLE_MIDDLEBOX_SERVICE)} -o yaml",
        check=False,
    )
    pod = run(
        f"kubectl -n {quote(NAMESPACE)} get pod {quote(SINGLE_MIDDLEBOX_POD)} -o wide",
        check=False,
    )
    raise RuntimeError(
        f"NodePort non raggiungibile su {host}:{port} dopo {timeout_s}s (last_error={last_error}).\n"
        f"Service:\n{svc[-2000:]}\n\n"
        f"Endpoints:\n{ep[-3000:]}\n\n"
        f"Pod:\n{pod[-2000:]}"
    )


def collect_middlebox_logs() -> str:
    logs = run(
        f"kubectl -n {quote(NAMESPACE)} logs {quote(SINGLE_MIDDLEBOX_POD)} --tail=500",
        check=False,
    )
    if logs:
        append_text(MIDDLEBOX_RUNTIME_LOG, logs)
    return logs


############################################
# THROUGHPUT TEST
############################################

def run_throughput_test(rate_ms: int, target: str) -> dict:
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
            print(f"  <-- Richiesta {req_num} timeout client ({request_timeout}s) verso {target}")
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
                baseline_ms = BASELINE_SERVICE_TIME_MS.get(SCENARIO, 11.0)
                queue_ms = max(0.0, service_ms - baseline_ms)
                with results_lock:
                    latencies_by_req[req_index] = lat
                    queue_delay_by_req_ms[req_index] = queue_ms
                    status_by_req[req_index] = "ok"
                print(
                    f"  <-- Richiesta {req_num} completata: "
                    f"servizio={service_ms:.2f}ms, coda={queue_ms:.2f}ms, dispatch_delay={dispatch_delay_ms:.2f}ms"
                )
                return
            with results_lock:
                status_by_req[req_index] = "invalid_latency"
            return

        if "Client: Error during request:" in output:
            print(f"  <-- Richiesta {req_num} client_error output:\n{output[:600]}")
            if req_num <= 3:
                mb_tail = collect_middlebox_logs()
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
    dispatch_delays_ok = [
        dispatch_delay_by_req_ms[i]
        for i in range(total_requests)
        if status_by_req[i] == "ok" and dispatch_delay_by_req_ms[i] is not None
    ]
    queue_delays_ok = [
        queue_delay_by_req_ms[i]
        for i in range(total_requests)
        if status_by_req[i] == "ok" and queue_delay_by_req_ms[i] is not None
    ]
    missing = total_requests - len(latencies)
    missing_timeout = sum(1 for status in status_by_req if status == "timeout")
    missing_no_timestamp = sum(1 for status in status_by_req if status == "no_timestamp")
    missing_client_error = sum(1 for status in status_by_req if status == "client_error")
    missing_other = sum(1 for status in status_by_req if status in ("error", "invalid_latency", "pending"))

    print(f"  Rate {rate_ms}ms: {len(latencies)}/{total_requests} latenze raccolte")
    if missing > 0:
        print(
            f"  Avviso missing={missing}: timeout={missing_timeout}, "
            f"client_error={missing_client_error}, no_timestamp={missing_no_timestamp}, other={missing_other}"
        )

    return {
        "rate_ms": rate_ms,
        "scenario": SCENARIO,
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


def run_throughput_suite() -> None:
    print(f"\n===== SCENARIO {SCENARIO} =====")

    nodeport = get_service_nodeport(SINGLE_MIDDLEBOX_SERVICE)
    host = get_node_host()
    wait_for_nodeport_reachable(host, nodeport, timeout_s=60)
    target = f"{host}:{nodeport}"

    results = []
    raw_rows = []

    for rate_ms in RATES:
        print(f"\n--- Rate {rate_ms}ms ({1000/rate_ms:.2f} Hz) ---")
        planned_requests = compute_requests_for_rate(rate_ms)
        print(f"  Finestra {TIME_REQUESTS_SECONDS}s -> richieste pianificate: {planned_requests}")

        test_result = run_throughput_test(rate_ms, target)
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
        dispatch_stats = compute_stats([int(value * 1_000_000) for value in dispatch_delays_ms])
        queue_stats = compute_stats([int(value * 1_000_000) for value in queue_delays_ms])

        total_latencies_ns = []
        for index, lat_ns in enumerate(latencies):
            lat_ms = lat_ns / 1_000_000
            if index < len(queue_delays_ms):
                total_latencies_ns.append(int((lat_ms + queue_delays_ms[index]) * 1_000_000))
            else:
                total_latencies_ns.append(lat_ns)
        total_stats = compute_stats(total_latencies_ns)

        result_row = {
            "scenario": SCENARIO,
            "rate_ms": rate_ms,
            "frequency_hz": round(1000 / rate_ms, 2),
            "num_requests": total_requests,
            "num_requests_ok": stats["count"],
            "missing_requests": missing,
            "missing_timeout": missing_timeout,
            "missing_client_error": missing_client_error,
            "missing_no_timestamp": missing_no_timestamp,
            "missing_other": missing_other,
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
            dispatch_ms = dispatch_delay_by_req_ms[req_idx - 1] if req_idx - 1 < len(dispatch_delay_by_req_ms) else None
            queue_ms = queue_delay_by_req_ms[req_idx - 1] if req_idx - 1 < len(queue_delay_by_req_ms) else None
            status = status_by_req[req_idx - 1] if req_idx - 1 < len(status_by_req) else "pending"
            raw_rows.append(
                {
                    "scenario": SCENARIO,
                    "rate_ms": rate_ms,
                    "request_index": req_idx,
                    "status": status,
                    "service_latency_ms": round(svc_ns / 1_000_000, 6) if svc_ns is not None else "",
                    "dispatch_delay_ms": round(dispatch_ms, 6) if dispatch_ms is not None else "",
                    "queue_delay_ms": round(queue_ms, 6) if queue_ms is not None else "",
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

        write_results_csv(results, raw_rows)

    write_results_csv(results, raw_rows)


############################################
# MAIN
############################################

def main() -> None:
    server_process: subprocess.Popen[str] | None = None
    try:
        ensure_directories()
        cleanup_old_output_files()
        ensure_client_binary()
        ensure_middlebox_sgx_binary()
        ensure_sgx_artifacts_consistency()
        ensure_kubernetes_cluster_ready()
        build_sgx_image()
        maybe_load_kind_images()
        server_process = start_server_process()

        print("\n[SETUP] Kubernetes middlebox SGX singolo scenario 1/2...")
        create_server_bridge()
        create_single_middlebox_sgx_pod(force_regenerate_each_request=(SCENARIO == 1))

        try:
            run_throughput_suite()
        finally:
            delete_single_middlebox_sgx_pod()
            delete_server_bridge()
            stop_server_process(server_process)

        print("\n✓ Test throughput SGX Kubernetes completato")

    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            stop_server_process(server_process)
            delete_single_middlebox_sgx_pod()
            delete_server_bridge()
        except Exception:
            pass


if __name__ == "__main__":
    main()