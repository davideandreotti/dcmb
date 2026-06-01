#!/usr/bin/env python3
from __future__ import annotations

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
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path
import csv


# Array di rate (ms tra richieste)
RATES = [25, 20, 25, 10, 7, 5, 2]

# Finestra temporale (secondi) entro cui inviare tutte le richieste possibili per ogni rate
TIME_REQUESTS_SECONDS = 30

# Scenario: 1 = delega reset, 2 = delega cache, 3 = orchestrazione swarm
SCENARIO = 2

# Network Docker
NETWORK = "tlmsp-net"

# Parametri scenario 3 (Docker Swarm), allineati a MisureOrchestrate.py
STACK_NAME = "tlmsp"
OVERLAY_NETWORK_NAME = "tlmsp_pool_overlay"
INITIAL_OPERATORS = 50
MIN_READY_OPERATORS = 30
SCALE_UP_BY = 30
AUTOSCALE_RATE_MS = 100
NO_READY_OPERATOR_POLICY = "wait"
NO_READY_OPERATOR_WAIT_SECONDS = 120

SWARM_CLIENT_CONTAINER = "client_orchestrate_throughput"
SWARM_SERVER_CONTAINER = "server_orchestrate_throughput"

# Percorsi
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

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"
CLIENT_BINARY = MB_DIR / "client"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"

TOKEN = "token"

BASE_DIR = PROJECT_ROOT / "ThroughputDocker"
RESULTS_DIR = BASE_DIR / "Risultati"
RUNTIME_DIR = BASE_DIR / "Runtime"
GRAFICI_DIR = BASE_DIR / "Grafici"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"

MIDDLEBOX_CONTAINER = "middlebox"
MIDDLEBOX_IMAGE = "middlebox"

# In questo scenario Docker il traffico resta baremetal lato client/server.
# Il solo componente containerizzato e' il middlebox.

STARTUP_TIMEOUT = 60
SETTLE_TIMEOUT = 20
SETTLE_INTERVAL = 0.5
SETTLE_STABLE_FOR = 1.5

# Timeout richieste client (richiesto: 120s)
CLIENT_REQUEST_TIMEOUT_SECONDS = 120
MAX_CLIENT_WORKERS = 128
# Baseline service time (t10-t1 medio da Misure.py per ogni scenario)
# Estratti da: ./Misure/Analisi/Risultati.txt
BASELINE_SERVICE_TIME_MS = {
    1: 8.1035,  # Operazione 2: t10-t1 average = 8103535 ns (da Misure.py AVERAGE operazione_N_2)
    2: 10.3,    # Operazione 3: t10-t1 average = 10299786 ns (da Misure.py AVERAGE operazione_N_3)
    3: 11.0,    # Scenario 3: baseline stimato (verrà aggiornato con dati reali da MisureOrchestrate.py)
}
TIMESTAMP_PATTERN = re.compile(r"\bt(\d+)\b\s*:\s*[^\n\r]*?=\s*(\d+)")

# Cache environment once: repeated docker info probes on every command are expensive,
# especially with the scenario-3 refill loop.
_RUN_ENV_CACHE: dict[str, str] | None = None
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
        "ss -ltnp | awk 'NR==1 || $4 ~ /:5000$/ || $4 ~ /:8000$/ || $4 ~ /:8443$/'",
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
                    diagnostics = f"\nDiagnostica porte locali (5000/8000/8443):\n{details}\n"
            raise RuntimeError(
                f"Il server locale è terminato inaspettatamente. Log:\n{content[-2000:]}{diagnostics}"
            )
        time.sleep(0.1)

    server_log.close()
    process.terminate()
    details = get_local_port_conflict_diagnostics()
    extra = f"\nDiagnostica porte locali (5000/8000/8443):\n{details}" if details else ""
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


def get_run_env() -> dict[str, str]:
    global _RUN_ENV_CACHE
    if _RUN_ENV_CACHE is not None:
        return _RUN_ENV_CACHE

    env = os.environ.copy()
    result = subprocess.run(
        "docker info --format '{{.ServerVersion}}'",
        shell=True,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode == 0:
        _RUN_ENV_CACHE = env
        return _RUN_ENV_CACHE
    fallback_socket = Path("/var/run/docker.sock")
    if fallback_socket.exists():
        fallback_env = env.copy()
        fallback_env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
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


def parse_ordered_times(text: str) -> list[tuple[int, int]]:
    times: list[tuple[int, int]] = []
    for line in text.splitlines():
        for match in TIMESTAMP_PATTERN.finditer(line):
            times.append((int(match.group(1)), int(match.group(2))))
    return times


def extract_latencies_from_logs() -> list[int]:
    """
    Estrae le latenze t10-t1 dal log del client (tutte le richieste del blocco corrente).
    Ritorna lista di latenze in ns.
    """
    client_text = read_text(CLIENT_RUNTIME_LOG)
    
    # Cerca tutti i marker "t1:" e "t10:" nel log
    t1_matches = re.findall(r"t1: \[CLIENT\][^=]*= (\d+) ns", client_text)
    t10_matches = re.findall(r"t10: \[CLIENT\][^=]*= (\d+) ns", client_text)
    
    latencies = []
    for i in range(min(len(t1_matches), len(t10_matches))):
        t1_ns = int(t1_matches[i])
        t10_ns = int(t10_matches[i])
        latency = t10_ns - t1_ns
        if latency > 0:
            latencies.append(latency)
    
    return latencies


############################################
# DOCKER SETUP
############################################

def cleanup_containers() -> None:
    for container in (MIDDLEBOX_CONTAINER,):
        run(f"docker rm -f {quote(container)}", check=False)


def build_images() -> None:
    print(f"!!! BUILD MIDDLEBOX IMAGE - START : {time.time_ns()}")
    run(
        f"docker build -f {quote(MB_DIR / 'Dockerfile.middlebox')} -t middlebox .",
        cwd=PROJECT_ROOT,
    )
    print(f"!!! BUILD MIDDLEBOX IMAGE - END : {time.time_ns()}")


def build_swarm_images() -> None:
    print(f"!!! BUILD SWARM IMAGES - START : {time.time_ns()}")
    run(
        f"docker build -f {quote(MB_DIR / 'Dockerfile.gateway')} -t mb_gateway:latest .",
        cwd=PROJECT_ROOT,
    )
    run(
        f"docker build -f {quote(MB_DIR / 'Dockerfile.operator')} -t mb_operator:latest .",
        cwd=PROJECT_ROOT,
    )
    print(f"!!! BUILD SWARM IMAGES - END : {time.time_ns()}")


def ensure_swarm() -> None:
    state = run("docker info --format '{{.Swarm.LocalNodeState}}'", check=False).strip().lower()
    if state == "active":
        return
    run("docker swarm init", check=True)


def ensure_overlay_network() -> None:
    result = subprocess.run(
        f"docker network inspect {quote(OVERLAY_NETWORK_NAME)}",
        shell=True,
        capture_output=True,
        text=True,
        env=get_run_env(),
    )
    if result.returncode != 0:
        run(f"docker network create --driver overlay --attachable {quote(OVERLAY_NETWORK_NAME)}")


def deploy_swarm_pool() -> None:
    env = {
        "STACK_NAME": STACK_NAME,
        "OVERLAY_NETWORK_NAME": OVERLAY_NETWORK_NAME,
        "WARM_REPLICAS": str(INITIAL_OPERATORS),
        "MIN_READY_OPERATORS": str(MIN_READY_OPERATORS),
        "SCALE_UP_BY": str(SCALE_UP_BY),
        # Manteniamo il parametro per compatibilità, ma il refill reale è gestito
        # da loop Python a intervallo millisecondi (AUTOSCALE_RATE_MS).
        "AUTOSCALE_PERIOD_SECONDS": "1",
        "NO_READY_OPERATOR_POLICY": NO_READY_OPERATOR_POLICY,
        "NO_READY_OPERATOR_WAIT_SECONDS": str(NO_READY_OPERATOR_WAIT_SECONDS),
        "POOL_STREAM_LOGS": "0",
        "POOL_CLEANUP_ON_EXIT": "0",
        "BUILD_IMAGES": "0",
        "OPERATOR_EXIT_AFTER_REQUEST": "true",
        "OPERATOR_CONSUME_AFTER_REQUEST": "true",
    }
    exports = " ".join(f"{k}={quote(v)}" for k, v in env.items())
    command = f"{exports} bash {quote(MB_DIR / 'run_middlebox_pool.sh')}"
    run(command, cwd=MB_DIR)


def cleanup_swarm_stack() -> None:
    run(f"docker stack rm {quote(STACK_NAME)}", check=False)


def warm_service_name() -> str:
    return f"{STACK_NAME}_mb_operator_warm"


def get_service_desired_replicas(service_name: str) -> int:
    result = subprocess.run(
        f"docker service inspect {quote(service_name)} --format '{{{{.Spec.Mode.Replicated.Replicas}}}}'",
        shell=True,
        capture_output=True,
        text=True,
        env=get_run_env(),
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Servizio Swarm non disponibile: {service_name}. Dettaglio: {output}")
    try:
        return int(output)
    except Exception:
        return 0


def get_service_running_tasks(service_name: str) -> int:
    result = subprocess.run(
        f"docker service ps {quote(service_name)} --filter desired-state=running --format '{{{{.CurrentState}}}}'",
        shell=True,
        capture_output=True,
        text=True,
        env=get_run_env(),
    )
    if result.returncode != 0:
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        raise RuntimeError(f"Servizio Swarm non disponibile: {service_name}. Dettaglio: {output}")
    output = (result.stdout or "") + (result.stderr or "")
    count = 0
    for line in output.splitlines():
        if "Running" in line:
            count += 1
    return count


def set_refill_period_for_rate(rate_ms: int) -> int:
    """
    Refill loop a frequenza pari al 45% del rate richieste.
    Esempio: rate=100ms -> refill=45ms.
    """
    global CURRENT_REFILL_PERIOD_MS
    CURRENT_REFILL_PERIOD_MS = max(1, int(rate_ms * 0.45))
    return CURRENT_REFILL_PERIOD_MS


def wait_for_swarm_pool_ready(min_running: int) -> None:
    """
    Attesa bloccante (senza timeout): il prossimo rate parte solo quando
    gateway e pool warm sono pronti.
    """
    warm_service = warm_service_name()
    gateway_service = f"{STACK_NAME}_mb_gateway"

    while True:
        warm_running = get_service_running_tasks(warm_service)
        warm_desired = get_service_desired_replicas(warm_service)
        gateway_running = get_service_running_tasks(gateway_service)
        gateway_desired = get_service_desired_replicas(gateway_service)

        print(
            f"[SCENARIO3][POOL] gateway={gateway_running}/{gateway_desired} "
            f"warm={warm_running}/{warm_desired} target>={min_running}"
        )

        if gateway_running >= 1 and warm_running >= min_running:
            return

        # Se il desired è sotto target, alziamo subito per pre-riscaldare il pool.
        if warm_desired < min_running:
            scale_service(warm_service, min_running)

        time.sleep(1.0)


def scale_service(service_name: str, replicas: int) -> None:
    run(f"docker service scale {quote(service_name)}={replicas}", check=False)


def run_swarm_refill_loop(stop_event: threading.Event) -> None:
    """
    Refill reale in millisecondi: ogni AUTOSCALE_RATE_MS controlla il numero
    di operator running e scala in alto se sotto soglia MIN_READY_OPERATORS.
    """
    service = warm_service_name()
    print(
        f"[SCENARIO3] refill-loop start: interval dinamico = 45% del rate "
        f"(iniziale {CURRENT_REFILL_PERIOD_MS}ms), min_ready={MIN_READY_OPERATORS}, scale_by={SCALE_UP_BY}"
    )

    while not stop_event.is_set():
        try:
            running = get_service_running_tasks(service)
            desired = get_service_desired_replicas(service)
            if running < MIN_READY_OPERATORS:
                target = max(desired, running) + SCALE_UP_BY
                print(
                    f"[SCENARIO3][REFILL] running={running} desired={desired} "
                    f"< min_ready={MIN_READY_OPERATORS} -> scale to {target}"
                )
                scale_service(service, target)
        except Exception as e:
            print(f"[SCENARIO3][REFILL] warning: {e}")

        interval_s = max(CURRENT_REFILL_PERIOD_MS, 1) / 1000.0
        stop_event.wait(interval_s)

    print("[SCENARIO3] refill-loop stop")


def start_idle_container(name: str, image: str) -> None:
    run_args = [
        "docker run -d",
        f"--name {quote(name)}",
        f"--network {quote(NETWORK)}",
        "--add-host server:host-gateway",
        "-p 8443:8443",
        f"-v {quote(str(BASE_DIR))}:/shared",
    ]

    run_args.extend([quote(image), "tail -f /dev/null"])
    run(" ".join(run_args))


def start_service(
    *,
    container: str,
    component: str,
    start_command: str,
    marker: str,
) -> None:
    log_path = {
        "server": SERVER_RUNTIME_LOG,
        "middlebox": MIDDLEBOX_RUNTIME_LOG,
    }[component]
    
    start_index = len(read_text(log_path))
    
    container_log_path = f"/shared/Runtime/{component.capitalize()}.log"
    inner_command = f"cd /app && {start_command} >> {quote(container_log_path)} 2>&1"
    
    run(f"docker exec -d {quote(container)} sh -lc {quote(inner_command)}")
    
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        content = read_text(log_path)
        if marker in content[start_index:]:
            return
        time.sleep(0.5)
    
    raise TimeoutError(f"Marker non trovato nel log {log_path}: {marker}")


def start_middlebox(force_regenerate_each_request: bool = False) -> None:
    command = "./middlebox"
    if force_regenerate_each_request:
        command = "MB_FORCE_REGENERATE_DC_EACH_REQUEST=1 ./middlebox"
    start_service(
        container=MIDDLEBOX_CONTAINER,
        component="middlebox",
        start_command=command,
        marker="[MB] Middlebox listening on :8443",
    )


def wait_for_tcp_reachable(host: str, port: int, timeout_s: int = 30) -> None:
    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.3)

    mb_tail = read_text(MIDDLEBOX_RUNTIME_LOG)[-3000:]
    raise RuntimeError(
        f"Middlebox non raggiungibile su {host}:{port} dopo {timeout_s}s (last_error={last_error}).\n"
        f"Middlebox log tail:\n{mb_tail}"
    )


def stop_service(container: str, process_pattern: str) -> None:
    kill_command = (
        f"pkill -INT -f {quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -TERM -f {quote(process_pattern)} 2>/dev/null || true; "
        "sleep 0.5; "
        f"pkill -KILL -f {quote(process_pattern)} 2>/dev/null || true"
    )
    run(f"docker exec {quote(container)} sh -lc {quote(kill_command)}", check=False)


def reset_middlebox_delegation() -> None:
    """Riavvia il middlebox senza eliminare la delega su disco."""
    stop_service(MIDDLEBOX_CONTAINER, "middlebox")
    run(
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc "
        f"{quote('pkill -9 -f middlebox 2>/dev/null || true')}",
        check=False,
    )
    start_middlebox()


############################################
# CLIENT REQUESTS
############################################

def client_request(target: str) -> str:
    url = f"https://{target}/function/init"
    output = run_client_process(build_client_command(target), timeout=STARTUP_TIMEOUT)
    
    append_text(CLIENT_RUNTIME_LOG, output)
    
    if "Calling:" not in output and '"status": "ok"' not in output:
        print(f"Avviso: richiesta verso {url} potrebbe non aver avuto successo")
    
    return output


############################################
# THROUGHPUT TEST
############################################

def run_throughput_test_scenario_1_or_2(scenario: int, rate_ms: int) -> dict:
    """
    Scenario 1: Delega reset serializzato dopo ogni completamento di richiesta.
    Scenario 2: Delega mantenuta (cache).

    Le richieste sono lanciate ogni rate_ms ms con threading.Timer (intervallo fisso),
    indipendentemente dal completamento delle precedenti.
    Quando rate < latenza_media, le richieste si accumulano in coda nel middlebox.
    Si attende che TUTTA la coda sia esaurita prima di tornare al loop dei rate.
    """
    reset_runtime_logs()
    total_requests = compute_requests_for_rate(rate_ms)

    latencies_by_req: list[int | None] = [None] * total_requests
    queue_delay_by_req_ms: list[float | None] = [None] * total_requests
    dispatch_delay_by_req_ms: list[float | None] = [None] * total_requests
    status_by_req: list[str] = ["pending"] * total_requests
    planned_send_time_by_req: list[float | None] = [None] * total_requests
    results_lock = threading.Lock()
    if scenario == 3:
        target = "127.0.0.1:8443"
        request_timeout = CLIENT_REQUEST_TIMEOUT_SECONDS
    else:
        target = "127.0.0.1:8443"
        request_timeout = CLIENT_REQUEST_TIMEOUT_SECONDS

    def fire_request(req_num: int) -> None:
        req_index = req_num - 1
        client_cmd = build_client_command(target)

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
            output = run_client_process(client_cmd, timeout=request_timeout)
        except subprocess.TimeoutExpired:
            print(
                f"  <-- Richiesta {req_num} timeout client ({request_timeout}s) "
                f"verso {target}"
            )
            with results_lock:
                status_by_req[req_index] = "timeout"
            return
        except Exception as e:
            print(f"  <-- Richiesta {req_num} errore esecuzione client: {e}")
            with results_lock:
                status_by_req[req_index] = "error"
            return
        append_text(CLIENT_RUNTIME_LOG, output)

        # Estrai t1 e t10 direttamente dall'output di questa richiesta.
        # Usiamo l'ULTIMA occorrenza nel log della richiesta per evitare match parziali.
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
                print(f"  <-- Richiesta {req_num} latenza non positiva")
                with results_lock:
                    status_by_req[req_index] = "invalid_latency"
        else:
            if "Client: Error during request:" in output:
                print(f"  <-- Richiesta {req_num} senza timestamp: client request error")
                with results_lock:
                    status_by_req[req_index] = "client_error"
            else:
                print(f"  <-- Richiesta {req_num} senza timestamp validi")
                with results_lock:
                    status_by_req[req_index] = "no_timestamp"

    # Lancia tutte le richieste possibili entro TIME_REQUESTS_SECONDS a intervallo fisso.
    # La coda reale resta lato middlebox.
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
    """
    Calcola statistiche su latenze (tutte in ns).
    Ritorna medie in ms.
    """
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
    """
    Esegue suite di test per uno scenario, variando rate.
    Salva risultati in CSV.
    """
    print(f"\n===== SCENARIO {scenario} =====")
    
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
            scale_service(warm_service_name(), INITIAL_OPERATORS)
            print(
                f"[SCENARIO3] rate={rate_ms}ms -> refill={refill_ms}ms (45%), "
                f"pool iniziale={INITIAL_OPERATORS}"
            )
            wait_for_swarm_pool_ready(INITIAL_OPERATORS)

        wait_for_tcp_reachable("127.0.0.1", 8443, timeout_s=30)
        
        try:
            test_result = run_throughput_test_scenario_1_or_2(scenario, rate_ms)
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
            
            # Calcola total_latencies_ms = service_latency_ms + queue_delay_ms
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
            
        except Exception as e:
            print(f"  ERRORE: {e}")

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
        build_images()
        server_process = start_server_process()

        if SCENARIO in (1, 2):
            ensure_network()
            cleanup_containers()
            print("\n[SETUP] Creazione container scenario 1/2...")
            start_idle_container(MIDDLEBOX_CONTAINER, MIDDLEBOX_IMAGE)
            # Scenario 1: forza rigenerazione delega ad ogni richiesta nel middlebox.
            start_middlebox(force_regenerate_each_request=(SCENARIO == 1))
        elif SCENARIO == 3:
            print("\n[SETUP] Creazione ambiente Docker Swarm scenario 3...")
            ensure_swarm()
            ensure_overlay_network()
            build_swarm_images()
            cleanup_swarm_stack()
            deploy_swarm_pool()
            refill_stop_event = threading.Event()
            refill_thread = threading.Thread(
                target=run_swarm_refill_loop,
                args=(refill_stop_event,),
                daemon=True,
            )
            refill_thread.start()
            # Verifica iniziale bloccante senza timeout.
            wait_for_swarm_pool_ready(INITIAL_OPERATORS)
        else:
            raise ValueError("SCENARIO deve essere 1, 2 o 3")
        
        # Esegui test per scenario
        try:
            run_throughput_suite(SCENARIO)
        finally:
            if SCENARIO in (1, 2):
                stop_service(MIDDLEBOX_CONTAINER, "middlebox")
                cleanup_containers()
            elif SCENARIO == 3:
                if refill_stop_event is not None:
                    refill_stop_event.set()
                if refill_thread is not None:
                    refill_thread.join(timeout=5)
                cleanup_swarm_stack()
            stop_server_process(server_process)
        
        print("\n✓ Test throughput completato")
    
    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            stop_server_process(server_process)
            if SCENARIO in (1, 2):
                cleanup_containers()
            elif SCENARIO == 3:
                if refill_stop_event is not None:
                    refill_stop_event.set()
                if refill_thread is not None and refill_thread.is_alive():
                    refill_thread.join(timeout=2)
                cleanup_swarm_stack()
        except Exception:
            pass


if __name__ == "__main__":
    main()
