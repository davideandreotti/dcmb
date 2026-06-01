from __future__ import annotations
import argparse
import os
import sys
import shlex
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime

############################################
# CONFIGURAZIONE
############################################

SSH_USER = os.environ.get("MT_SSH_USER", "bonsai")
SSH_KEY_PATH = Path(os.environ.get("MT_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519_masterthesis")))

CLIENT_HOST = os.environ.get("MT_CLIENT_HOST", "10.79.1.165")
MIDDLEBOX_HOST = os.environ.get("MT_MIDDLEBOX_HOST", "10.79.1.175")
SERVER_HOST = os.environ.get("MT_SERVER_HOST", "10.79.1.208")

# Configurazione primaria (modificabile direttamente nel file)
N_REQUESTS = 20
REQUEST_TYPE = "GET"
REQUEST_RATE_MS = 5000

# Keep a larger warm headroom because operators are single-use and get consumed
# at approximately request-rate pace.
INITIAL_OPERATORS = 15
MIN_READY_OPERATORS = 14
SCALE_UP_BY = 6
AUTOSCALE_RATE_MS = 300

# Override opzionali via environment
N_REQUESTS = int(os.environ.get("MT_N_REQUESTS", str(N_REQUESTS)))
REQUEST_TYPE = os.environ.get("MT_REQUEST_TYPE", REQUEST_TYPE).upper()
REQUEST_RATE_MS = int(os.environ.get("MT_REQUEST_RATE_MS", str(REQUEST_RATE_MS)))

INITIAL_OPERATORS = int(os.environ.get("MT_INITIAL_OPERATORS", str(INITIAL_OPERATORS)))
MIN_READY_OPERATORS = int(os.environ.get("MT_MIN_READY_OPERATORS", str(MIN_READY_OPERATORS)))
SCALE_UP_BY = int(os.environ.get("MT_SCALE_UP_BY", str(SCALE_UP_BY)))
AUTOSCALE_RATE_MS = int(os.environ.get("MT_AUTOSCALE_RATE_MS", str(AUTOSCALE_RATE_MS)))
NO_READY_OPERATOR_POLICY = os.environ.get("MT_NO_READY_OPERATOR_POLICY", "fail")
OPERATOR_EXIT_AFTER_REQUEST = "true"
OPERATOR_CONSUME_AFTER_REQUEST = "true"

STACK_NAME = os.environ.get("MT_STACK_NAME", "tlmsp")
OVERLAY_NETWORK_NAME = os.environ.get("MT_OVERLAY_NETWORK_NAME", "tlmsp_pool_overlay")
TOKEN = os.environ.get("MT_TOKEN", "token")
CLIENT_ID = os.environ.get("MT_CLIENT_ID", "client")

GATEWAY_HOST_FOR_CLIENT = os.environ.get("MT_GATEWAY_HOST_FOR_CLIENT", MIDDLEBOX_HOST)
GATEWAY_PORT = int(os.environ.get("MT_GATEWAY_PORT", "8443"))

# Topologia esplicita (client <-> middlebox <-> server)
CLIENT_MIDDLEBOX_FACING_IP = os.environ.get("MT_CLIENT_MIDDLEBOX_IP", "172.16.1.2")
MIDDLEBOX_CLIENT_FACING_IP = os.environ.get("MT_MIDDLEBOX_CLIENT_FACING_IP", "172.16.1.1")
MIDDLEBOX_SERVER_FACING_IP = os.environ.get("MT_MIDDLEBOX_SERVER_FACING_IP", "172.16.0.1")
SERVER_MIDDLEBOX_FACING_IP = os.environ.get("MT_SERVER_MIDDLEBOX_IP", "172.16.0.2")

# Swarm su middlebox multi-homed: indirizzo da pubblicare per il manager.
SWARM_ADVERTISE_ADDR = os.environ.get("MT_SWARM_ADVERTISE_ADDR", MIDDLEBOX_CLIENT_FACING_IP)

STARTUP_TIMEOUT = int(os.environ.get("MT_STARTUP_TIMEOUT", "180"))
STACK_REMOVE_TIMEOUT_S = int(os.environ.get("MT_STACK_REMOVE_TIMEOUT_S", "45"))

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT: Path | None = None
for candidate in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
    if (candidate / "PerformanceMeasuring").exists() and (candidate / "DC" / "Middlebox").exists():
        PROJECT_ROOT = candidate
        break
if PROJECT_ROOT is None:
    raise RuntimeError("Impossibile individuare PROJECT_ROOT: manca PerformanceMeasuring o DC/Middlebox")

MB_DIR = PROJECT_ROOT / "DC" / "Middlebox"
SERVER_DIR = PROJECT_ROOT / "PerformanceMeasuring"
GO_PROFESSOR = PROJECT_ROOT / "DC" / "go" / "bin" / "go"

CLIENT_BINARY = MB_DIR / "client"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"
DC_GENERATOR_SOURCE = PROJECT_ROOT / "DC" / "go" / "src" / "crypto" / "tls" / "generate_delegated_credential.go"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"
SERVER_CERT_HOST_PATH = SERVER_CERTS_HOST_DIR / "cert.pem"
SERVER_KEY_HOST_PATH = SERVER_CERTS_HOST_DIR / "key.pem"

BASE_DIR = PROJECT_ROOT / "Misure"
ANALYSIS_DIR = BASE_DIR / "Analisi"
RUNTIME_DIR = BASE_DIR / "Runtime"
ORCHESTRATE_LOG_DIR = BASE_DIR / "Orchestrate"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"
SUMMARY_FILE = ANALYSIS_DIR / "OrchestrateSummary.txt"

LOCAL_GENERATE_TOOL = RUNTIME_DIR / "generate_dc_tool"

REMOTE_PROJECT_ROOT = f"/home/{SSH_USER}/MasterThesis"
REMOTE_MB_DIR = f"{REMOTE_PROJECT_ROOT}/DC/Middlebox"
REMOTE_SERVER_DIR = f"{REMOTE_PROJECT_ROOT}/PerformanceMeasuring"
REMOTE_BASE_DIR = f"{REMOTE_PROJECT_ROOT}/Misure"
REMOTE_RUNTIME_DIR = f"{REMOTE_BASE_DIR}/Runtime"
REMOTE_ANALYSIS_DIR = f"{REMOTE_BASE_DIR}/Analisi"
REMOTE_ORCHESTRATE_DIR = f"{REMOTE_BASE_DIR}/Orchestrate"

REMOTE_CLIENT_BINARY = f"{REMOTE_MB_DIR}/client"
REMOTE_CLIENT_CA_PATH = f"{REMOTE_PROJECT_ROOT}/certs_external/ca.crt"
REMOTE_SERVER_SCRIPT = f"{REMOTE_SERVER_DIR}/certs_server.py"
REMOTE_SERVER_CERT_DIR = f"{REMOTE_PROJECT_ROOT}/certs_external/server"
REMOTE_GENERATE_TOOL = f"{REMOTE_RUNTIME_DIR}/generate_dc_tool"
REMOTE_SERVER_LOG = f"{REMOTE_RUNTIME_DIR}/Server.log"
REMOTE_SERVER_PIDFILE = f"{REMOTE_BASE_DIR}/server.pid"

GRAPHS_BASE_DIR = SCRIPT_DIR / "Grafici"
GRAPHS_SUMMARY_DIR = GRAPHS_BASE_DIR / "Summary"
GRAPHS_LATENCY_DIR = GRAPHS_BASE_DIR / "Latency"
GRAPHS_REFILL_DIR = GRAPHS_BASE_DIR / "Refill"

############################################
# UTILITY SSH/LOCALE
############################################


############################################
# UTILITY
############################################

@dataclass
class RequestResult:
    index: int
    ok: bool
    output: str
    started_ns: int
    completed_ns: int


def quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def is_localhost(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", CLIENT_HOST}


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def system_log(message: str) -> None:
    print(f"[{now_stamp()}] [SYSTEM] {message}", flush=True)


def run_local(
    command: str,
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> str:
    result = subprocess.run(
        command,
        shell=True,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if check and result.returncode != 0:
        raise RuntimeError(f"Comando locale fallito ({result.returncode}): {command}\n{output}")
    return output


def ssh_cmd(host: str) -> list[str]:
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
        "-o", "BatchMode=yes",
    ]
    if SSH_KEY_PATH.exists():
        cmd += ["-i", str(SSH_KEY_PATH)]
    cmd.append(f"{SSH_USER}@{host}")
    return cmd


def scp_cmd() -> list[str]:
    cmd = [
        "scp",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
        "-o", "BatchMode=yes",
    ]
    if SSH_KEY_PATH.exists():
        cmd += ["-i", str(SSH_KEY_PATH)]
    return cmd


def run_ssh(
    host: str,
    remote_command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
    print_command: bool = False,
) -> str:
    if is_localhost(host):
        if print_command:
            system_log(f"LOCAL: {remote_command}")
        return run_local(remote_command, check=check, timeout=timeout)
    if print_command:
        system_log(f"SSH {host}: {remote_command}")
    result = subprocess.run(
        ssh_cmd(host) + [remote_command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if check and result.returncode != 0:
        raise RuntimeError(f"Comando SSH fallito ({result.returncode}) su {host}: {remote_command}\n{output}")
    return output


def upload_file(host: str, local_path: Path, remote_path: str) -> None:
    if not local_path.exists():
        raise FileNotFoundError(f"File locale mancante: {local_path}")
    if is_localhost(host):
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, remote_path)
        return
    run_ssh(host, f"mkdir -p {quote(str(Path(remote_path).parent))}")
    result = subprocess.run(
        scp_cmd() + [str(local_path), f"{SSH_USER}@{host}:{remote_path}"],
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Upload fallito verso {host}:{remote_path}\n"
            f"{result.stdout or ''}{result.stderr or ''}"
        )


def append_local(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def read_local(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def wait_tcp(host: str, port: int, *, timeout_s: int = STARTUP_TIMEOUT) -> None:
    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise TimeoutError(f"Endpoint TCP non raggiungibile: {host}:{port}. Ultimo errore: {last_error}")


def get_warm_service_replica_state() -> tuple[int, int]:
    svc = f"{STACK_NAME}_mb_operator_warm"
    raw = run_ssh(
        MIDDLEBOX_HOST,
        f"docker service ls --filter name={quote(svc)} --format '{{{{.Replicas}}}}'",
        check=False,
    ).strip()
    if not raw or "/" not in raw:
        return 0, 0
    running_s, desired_s = raw.split("/", 1)
    try:
        return int(running_s.strip()), int(desired_s.strip())
    except ValueError:
        return 0, 0


def scale_warm_service(new_desired: int) -> None:
    svc = f"{STACK_NAME}_mb_operator_warm"
    run_ssh(MIDDLEBOX_HOST, f"docker service scale --detach=true {quote(svc)}={new_desired}", check=True)


def refill_manager(stop_event: threading.Event, events: list[tuple[int, int, int, str]]) -> None:
    interval = max(0.1, AUTOSCALE_RATE_MS / 1000.0)
    while not stop_event.is_set():
        running, desired = get_warm_service_replica_state()
        action = "check"
        system_log(
            f"[REFILL CHECK] running={running} desired={desired} threshold={MIN_READY_OPERATORS} scale_up={SCALE_UP_BY}"
        )

        if running < MIN_READY_OPERATORS:
            if desired > running:
                action = "provisioning"
                system_log(
                    f"[REFILL HOLD] provisioning in corso: running={running}, desired={desired}. Nessun nuovo scale."
                )
                events.append((time.time_ns(), running, desired, action))
                stop_event.wait(interval)
                continue

            new_desired = max(desired, running) + SCALE_UP_BY
            system_log(
                f"[REFILL TRIGGER] soglia raggiunta: running={running} < {MIN_READY_OPERATORS}; scale {desired} -> {new_desired}"
            )
            try:
                scale_warm_service(new_desired)
                action = "refill"
                system_log(f"[REFILL ACTIVE] scale richiesta inviata: warm replicas target={new_desired}")
            except Exception as exc:
                action = "refill_error"
                system_log(f"[REFILL ERROR] {exc}")

        events.append((time.time_ns(), running, desired, action))
        stop_event.wait(interval)

def wait_operator_pool_ready(min_running: int, *, timeout_s: int = STARTUP_TIMEOUT) -> None:
    svc = f"{STACK_NAME}_mb_operator_warm"
    deadline = time.time() + timeout_s
    last_snapshot = ""

    while time.time() < deadline:
        snapshot = run_ssh(
            MIDDLEBOX_HOST,
            f"docker service ps {quote(svc)} "
            "--filter desired-state=running "
            "--format '{{.ID}}|{{.CurrentState}}|{{.Error}}'",
            check=False,
        )
        last_snapshot = snapshot

        running = sum(1 for line in snapshot.splitlines() if "|Running" in line)
        if running >= min_running:
            system_log(f"Operator pool ready: running={running} >= {min_running}")
            return

        time.sleep(0.5)

    raise TimeoutError(
        f"Operator pool non pronto: running < {min_running}\n"
        f"Ultimo snapshot:\n{last_snapshot}\n"
        f"Diagnostica:\n{collect_middlebox_diagnostics()}"
    )


def preflight_swarm_environment() -> None:
    checks = [
        ("docker", "docker info >/dev/null && echo DOCKER_OK"),
        ("compose stack file", f"test -f {quote(REMOTE_MB_DIR + '/docker-stack.yml')} && echo STACK_FILE_OK"),
        ("pool script", f"test -x {quote(REMOTE_MB_DIR + '/run_middlebox_pool.sh')} && echo POOL_SCRIPT_OK"),
        ("gateway image", "docker image inspect mb_gateway:latest >/dev/null 2>&1 && echo GATEWAY_IMAGE_OK || true"),
        ("operator image", "docker image inspect mb_operator:latest >/dev/null 2>&1 && echo OPERATOR_IMAGE_OK || true"),
    ]

    failures: list[str] = []
    for name, cmd in checks:
        out = run_ssh(MIDDLEBOX_HOST, cmd, check=False)
        if "_OK" not in out:
            failures.append(f"{name}: {out.strip() or 'FAILED'}")

    if failures:
        raise RuntimeError("Preflight Docker Swarm fallito:\n- " + "\n- ".join(failures))

def reset_local_output_dirs() -> None:
    for directory in (RUNTIME_DIR, ANALYSIS_DIR, ORCHESTRATE_LOG_DIR):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    for directory in (GRAPHS_BASE_DIR, GRAPHS_SUMMARY_DIR, GRAPHS_LATENCY_DIR, GRAPHS_REFILL_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def ensure_local_artifacts() -> None:
    required = [
        SERVER_SCRIPT,
        SERVER_CERT_HOST_PATH,
        SERVER_KEY_HOST_PATH,
        CLIENT_CA_HOST_PATH,
        DC_GENERATOR_SOURCE,
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Artefatti locali mancanti:\n" + "\n".join(str(p) for p in missing))

    if not GO_PROFESSOR.exists():
        raise FileNotFoundError(f"Toolchain Go del professore non trovata: {GO_PROFESSOR}")

    if not CLIENT_BINARY.exists() or CLIENT_BINARY.stat().st_mtime < (MB_DIR / "client.go").stat().st_mtime:
        system_log("Compilazione client baremetal")
        run_local(f"{quote(GO_PROFESSOR)} build -o {quote(CLIENT_BINARY)} client.go", cwd=MB_DIR, timeout=180)

    LOCAL_GENERATE_TOOL.parent.mkdir(parents=True, exist_ok=True)
    if not LOCAL_GENERATE_TOOL.exists() or LOCAL_GENERATE_TOOL.stat().st_mtime < DC_GENERATOR_SOURCE.stat().st_mtime:
        system_log("Compilazione tool generate delegated credential")
        run_local(
            f"{quote(GO_PROFESSOR)} build -o {quote(LOCAL_GENERATE_TOOL)} {quote(DC_GENERATOR_SOURCE)}",
            timeout=180,
        )


def ensure_remote_layout() -> None:
    for host in (CLIENT_HOST, SERVER_HOST, MIDDLEBOX_HOST):
        run_ssh(host, f"mkdir -p {quote(REMOTE_RUNTIME_DIR)} {quote(REMOTE_ANALYSIS_DIR)} {quote(REMOTE_ORCHESTRATE_DIR)}")

    upload_file(CLIENT_HOST, CLIENT_BINARY, REMOTE_CLIENT_BINARY)
    upload_file(CLIENT_HOST, CLIENT_CA_HOST_PATH, REMOTE_CLIENT_CA_PATH)
    run_ssh(CLIENT_HOST, f"chmod +x {quote(REMOTE_CLIENT_BINARY)}")

    upload_file(SERVER_HOST, SERVER_SCRIPT, REMOTE_SERVER_SCRIPT)
    upload_file(SERVER_HOST, SERVER_CERT_HOST_PATH, f"{REMOTE_SERVER_CERT_DIR}/cert.pem")
    upload_file(SERVER_HOST, SERVER_KEY_HOST_PATH, f"{REMOTE_SERVER_CERT_DIR}/key.pem")
    upload_file(SERVER_HOST, LOCAL_GENERATE_TOOL, REMOTE_GENERATE_TOOL)
    run_ssh(SERVER_HOST, f"chmod +x {quote(REMOTE_GENERATE_TOOL)} && chmod 600 {quote(f'{REMOTE_SERVER_CERT_DIR}/key.pem')}")

    # Allinea i file di orchestrazione middlebox sul remoto per evitare drift tra repository.
    upload_file(MIDDLEBOX_HOST, MB_DIR / "run_middlebox_pool.sh", f"{REMOTE_MB_DIR}/run_middlebox_pool.sh")
    upload_file(MIDDLEBOX_HOST, MB_DIR / "docker-stack.yml", f"{REMOTE_MB_DIR}/docker-stack.yml")
    upload_file(MIDDLEBOX_HOST, MB_DIR / "Dockerfile.gateway", f"{REMOTE_MB_DIR}/Dockerfile.gateway")
    upload_file(MIDDLEBOX_HOST, MB_DIR / "Dockerfile.operator", f"{REMOTE_MB_DIR}/Dockerfile.operator")
    run_ssh(MIDDLEBOX_HOST, f"chmod +x {quote(REMOTE_MB_DIR + '/run_middlebox_pool.sh')}")


############################################
# SERVER BAREMETAL
############################################

def start_server_baremetal() -> str:
    stop_server_baremetal(None)
    run_ssh(SERVER_HOST, f": > {quote(REMOTE_SERVER_LOG)}")

    command = (
        "set -e; "
        f"cd {quote(REMOTE_PROJECT_ROOT)}; "
        f"GO_TOOL={quote(REMOTE_GENERATE_TOOL)} "
        f"nohup python3 -u {quote(REMOTE_SERVER_SCRIPT)} "
        f">> {quote(REMOTE_SERVER_LOG)} 2>&1 < /dev/null & "
        f"echo $! > {quote(REMOTE_SERVER_PIDFILE)}; "
        f"cat {quote(REMOTE_SERVER_PIDFILE)}"
    )
    pid = run_ssh(SERVER_HOST, command, timeout=30).strip().splitlines()[-1]

    marker = "[SERVER] Application TLS server running on :8000"
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        log = run_ssh(SERVER_HOST, f"cat {quote(REMOTE_SERVER_LOG)} 2>/dev/null || true", check=False)
        if marker in log:
            system_log(f"Server baremetal avviato su {SERVER_HOST}:8000")
            return pid
        time.sleep(0.5)

    raise TimeoutError(f"Server baremetal non pronto. Log:\n{log[-4000:]}")


def stop_server_baremetal(pid: str | None) -> None:
    if pid:
        run_ssh(SERVER_HOST, f"kill {quote(pid)} >/dev/null 2>&1 || true", check=False)
    run_ssh(
        SERVER_HOST,
        f"if [ -f {quote(REMOTE_SERVER_PIDFILE)} ]; then "
        f"kill $(cat {quote(REMOTE_SERVER_PIDFILE)}) >/dev/null 2>&1 || true; "
        f"rm -f {quote(REMOTE_SERVER_PIDFILE)}; "
        "fi; "
        "pkill -f '[c]erts_server.py' >/dev/null 2>&1 || true",
        check=False,
    )


############################################
# MIDDLEBOX DOCKER SWARM
############################################

def ensure_swarm_on_middlebox() -> None:
    state = run_ssh(
        MIDDLEBOX_HOST,
        "docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || true",
        check=False,
    ).strip().lower()

    if state == "active":
        return

    # Ordine di preferenza: configurazione esplicita -> lato client -> lato server -> IP pubblico.
    candidates: list[str] = []
    for candidate in [SWARM_ADVERTISE_ADDR, MIDDLEBOX_CLIENT_FACING_IP, MIDDLEBOX_SERVER_FACING_IP, MIDDLEBOX_HOST]:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    system_log(f"Inizializzazione Docker Swarm sul middlebox (candidate advertise-addr: {', '.join(candidates)})")

    last_out = ""
    for advertise_addr in candidates:
        init_cmd = f"docker swarm init --advertise-addr {quote(advertise_addr)}"
        out = run_ssh(MIDDLEBOX_HOST, init_cmd, check=False, timeout=120)
        last_out = out

        if (
            "Swarm initialized" in out
            or "This node joined a swarm" in out
            or "This node is already part of a swarm" in out
            or "already part of a swarm" in out
        ):
            system_log(f"Docker Swarm inizializzato su {MIDDLEBOX_HOST} con advertise-addr={advertise_addr}")
            return

        system_log(f"Tentativo swarm init fallito con advertise-addr={advertise_addr}")

    raise RuntimeError(
        f"docker swarm init fallito su {MIDDLEBOX_HOST} con tutti gli advertise-addr candidati. "
        f"Ultimo output:\n{last_out}"
    )


def cleanup_swarm_pool() -> None:
    run_ssh(MIDDLEBOX_HOST, f"docker stack rm {quote(STACK_NAME)} >/dev/null 2>&1 || true", check=False)

    deadline = time.time() + STACK_REMOVE_TIMEOUT_S
    while time.time() < deadline:
        services = run_ssh(
            MIDDLEBOX_HOST,
            f"docker stack services {quote(STACK_NAME)} --format '{{{{.Name}}}}' 2>/dev/null || true",
            check=False,
        ).strip()
        if not services:
            return
        time.sleep(0.5)

    system_log(f"Avvertenza: stack {STACK_NAME} non rimosso entro {STACK_REMOVE_TIMEOUT_S}s")


def start_middlebox_swarm_pool() -> None:
    ensure_swarm_on_middlebox()
    cleanup_swarm_pool()

    # Build immagini con context repository root: i Dockerfile usano COPY DC/...
    # e quindi falliscono se il context e' DC/Middlebox.
    system_log("Pre-build immagini middlebox con context remoto corretto")
    run_ssh(
        MIDDLEBOX_HOST,
        f"docker build -f {quote(REMOTE_MB_DIR + '/Dockerfile.gateway')} -t mb_gateway:latest {quote(REMOTE_PROJECT_ROOT)}",
        timeout=1800,
    )
    run_ssh(
        MIDDLEBOX_HOST,
        f"docker build -f {quote(REMOTE_MB_DIR + '/Dockerfile.operator')} -t mb_operator:latest {quote(REMOTE_PROJECT_ROOT)}",
        timeout=1800,
    )

    preflight_swarm_environment()

    env = {
        "STACK_NAME": STACK_NAME,
        "OVERLAY_NETWORK_NAME": OVERLAY_NETWORK_NAME,
        "WARM_REPLICAS": str(INITIAL_OPERATORS),
        "MIN_READY_OPERATORS": str(MIN_READY_OPERATORS),
        "SCALE_UP_BY": str(SCALE_UP_BY),
        "AUTOSCALE_PERIOD_SECONDS": str(max(1, AUTOSCALE_RATE_MS // 1000)),
        "POOL_STREAM_LOGS": "0",
        "POOL_CLEANUP_ON_EXIT": "0",
        "BUILD_IMAGES": "0",

        # Non modifica la semantica di deleghe/autenticazione: ogni operator è single-use.
        "NO_READY_OPERATOR_POLICY": NO_READY_OPERATOR_POLICY,
        "OPERATOR_EXIT_AFTER_REQUEST": OPERATOR_EXIT_AFTER_REQUEST,
        "OPERATOR_CONSUME_AFTER_REQUEST": OPERATOR_CONSUME_AFTER_REQUEST,

        # Server baremetal remoto.
        "BAREMETAL_SERVER_HOST": SERVER_HOST,
        "SERVER_HOST": SERVER_HOST,
        "SERVER_ADDR": SERVER_HOST,
        "MB_SERVER_HOST": SERVER_HOST,
        "MB_TARGET_URL": f"https://{SERVER_HOST}:8000",
        "MB_CERT_URL": f"http://{SERVER_HOST}:5000",
        "MB_UPSTREAM_SERVER_NAME": "server",

        "MB_MINIMAL_LOGS": "1",
    }

    exports = " ".join(f"{key}={quote(value)}" for key, value in env.items())
    command = f"cd {quote(REMOTE_MB_DIR)} && {exports} bash run_middlebox_pool.sh"

    system_log("Avvio pool middlebox Docker Swarm")
    system_log(
        f"Pool operator: initial={INITIAL_OPERATORS}, threshold={MIN_READY_OPERATORS}, "
        f"scale_up_by={SCALE_UP_BY}, autoscale={AUTOSCALE_RATE_MS}ms"
    )
    run_ssh(MIDDLEBOX_HOST, command, timeout=900)

    wait_tcp(GATEWAY_HOST_FOR_CLIENT, GATEWAY_PORT, timeout_s=STARTUP_TIMEOUT)
    wait_operator_pool_ready(MIN_READY_OPERATORS, timeout_s=STARTUP_TIMEOUT)
    system_log(f"Gateway middlebox raggiungibile su {GATEWAY_HOST_FOR_CLIENT}:{GATEWAY_PORT}")


def collect_middlebox_diagnostics() -> str:
    chunks = [
        "=== docker service ls ===",
        run_ssh(MIDDLEBOX_HOST, "docker service ls", check=False),
        "=== docker stack services ===",
        run_ssh(MIDDLEBOX_HOST, f"docker stack services {quote(STACK_NAME)}", check=False),
        "=== gateway tasks ===",
        run_ssh(MIDDLEBOX_HOST, f"docker service ps {quote(STACK_NAME + '_mb_gateway')} --no-trunc", check=False),
        "=== operator tasks ===",
        run_ssh(MIDDLEBOX_HOST, f"docker service ps {quote(STACK_NAME + '_mb_operator_warm')} --no-trunc", check=False),
        "=== gateway logs tail ===",
        run_ssh(MIDDLEBOX_HOST, f"docker service logs --tail 150 {quote(STACK_NAME + '_mb_gateway')}", check=False),
        "=== operator logs tail ===",
        run_ssh(MIDDLEBOX_HOST, f"docker service logs --tail 150 {quote(STACK_NAME + '_mb_operator_warm')}", check=False),
    ]
    return "\n".join(chunks)


############################################
# CLIENT BAREMETAL
############################################

def send_client_request(index: int) -> RequestResult:
    url = f"https://{GATEWAY_HOST_FOR_CLIENT}:{GATEWAY_PORT}/function/init"
    started_ns = time.time_ns()

    parts = [
        quote(REMOTE_CLIENT_BINARY),
        "-id", quote(CLIENT_ID),
        "-ca", quote(REMOTE_CLIENT_CA_PATH),
        "-servername", "server",
        "-H", quote(f"Authorization : Bearer {TOKEN}"),
    ]

    if REQUEST_TYPE == "POST":
        parts.extend(["-data", quote("{}")])
    elif REQUEST_TYPE != "GET":
        raise ValueError("REQUEST_TYPE deve essere GET oppure POST")

    parts.append(quote(url))

    remote_command = f"cd {quote(REMOTE_MB_DIR)} && {' '.join(parts)}"
    output = run_ssh(CLIENT_HOST, remote_command, check=False, timeout=STARTUP_TIMEOUT)
    completed_ns = time.time_ns()

    ok = ('"status": "ok"' in output) or ('"status":"ok"' in output)
    return RequestResult(index=index, ok=ok, output=output, started_ns=started_ns, completed_ns=completed_ns)


############################################
# LOG E RISULTATI
############################################

def append_experiment_log(result: RequestResult) -> None:
    marker = f"\n===== ESPERIMENTO {result.index} =====\n--- OPERAZIONE 1 (esperimento {result.index}) ---\n"
    append_local(CLIENT_RUNTIME_LOG, marker)
    append_local(CLIENT_RUNTIME_LOG, f"t31: [ORCH] - request_dispatched = {result.started_ns} ns\n")
    append_local(CLIENT_RUNTIME_LOG, result.output)
    if not result.output.endswith("\n"):
        append_local(CLIENT_RUNTIME_LOG, "\n")
    append_local(CLIENT_RUNTIME_LOG, f"t32: [ORCH] - request_completed = {result.completed_ns} ns\n")

    append_local(
        MIDDLEBOX_RUNTIME_LOG,
        f"\n===== ESPERIMENTO {result.index} =====\n"
        f"--- OPERAZIONE 1 (esperimento {result.index}) ---\n"
        f"[ORCH] request_ok={result.ok}\n",
    )


def sync_server_log() -> None:
    content = run_ssh(SERVER_HOST, f"cat {quote(REMOTE_SERVER_LOG)} 2>/dev/null || true", check=False)
    SERVER_RUNTIME_LOG.parent.mkdir(parents=True, exist_ok=True)
    SERVER_RUNTIME_LOG.write_text(content, encoding="utf-8")


def write_summary(ok: list[int], errors: list[int]) -> None:
    SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SUMMARY_FILE.open("w", encoding="utf-8") as handle:
        handle.write("Misure orchestrate con client/server baremetal e middlebox Docker Swarm\n")
        handle.write("====================================================================\n")
        handle.write(f"Client host: {CLIENT_HOST}\n")
        handle.write(f"Middlebox host: {MIDDLEBOX_HOST}\n")
        handle.write(f"Server host: {SERVER_HOST}\n")
        handle.write(f"Gateway endpoint client: {GATEWAY_HOST_FOR_CLIENT}:{GATEWAY_PORT}\n")
        handle.write(f"Richieste schedulate: {N_REQUESTS}\n")
        handle.write(f"OK: {len(ok)}\n")
        handle.write(f"Errori: {len(errors)}\n")
        handle.write(f"Messaggi OK: {', '.join(map(str, ok)) if ok else '-'}\n")
        handle.write(f"Messaggi errore: {', '.join(map(str, errors)) if errors else '-'}\n")
        handle.write(f"Tipo richiesta: {REQUEST_TYPE}\n")
        handle.write(f"Rate client: 1 ogni {REQUEST_RATE_MS} ms\n")
        handle.write(f"Pool iniziale operator: {INITIAL_OPERATORS}\n")
        handle.write(f"Soglia autoscale: {MIN_READY_OPERATORS}\n")
        handle.write(f"Scale up by: {SCALE_UP_BY}\n")
        handle.write(f"Rate autoscale: ogni {AUTOSCALE_RATE_MS} ms\n")


def generate_graphs(
    latencies_ms: list[float],
    refill_events: list[tuple[int, int, int, str]],
    ok_count: int,
    error_count: int,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        system_log(f"Grafici non generati: matplotlib non disponibile ({exc})")
        return

    plt.figure(figsize=(7, 4))
    plt.bar(["OK", "ERROR"], [ok_count, error_count], color=["#2ca02c", "#d62728"])
    plt.title("Esito richieste")
    plt.ylabel("Conteggio")
    plt.tight_layout()
    plt.savefig(GRAPHS_SUMMARY_DIR / "requests_outcome.png", dpi=150)
    plt.close()

    if latencies_ms:
        plt.figure(figsize=(9, 4))
        x = list(range(1, len(latencies_ms) + 1))
        plt.plot(x, latencies_ms, marker="o", linewidth=1.5)
        plt.title("Latenza richiesta per indice")
        plt.xlabel("Richiesta")
        plt.ylabel("Latenza (ms)")
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(GRAPHS_LATENCY_DIR / "request_latency_ms.png", dpi=150)
        plt.close()

    if refill_events:
        t0 = refill_events[0][0]
        xs = [(ts - t0) / 1_000_000_000 for ts, _, _, _ in refill_events]
        running = [r for _, r, _, _ in refill_events]
        desired = [d for _, _, d, _ in refill_events]

        plt.figure(figsize=(10, 4.2))
        plt.plot(xs, running, label="running warm", linewidth=1.5)
        plt.plot(xs, desired, label="desired warm", linewidth=1.5)
        plt.axhline(MIN_READY_OPERATORS, color="#ff7f0e", linestyle="--", label="threshold")
        for ts, r, d, action in refill_events:
            if action == "refill":
                x = (ts - t0) / 1_000_000_000
                plt.axvline(x, color="#d62728", alpha=0.25)
        plt.title("Controllo soglia e refill warm pool")
        plt.xlabel("Tempo (s)")
        plt.ylabel("Repliche")
        plt.legend()
        plt.grid(True, alpha=0.25)
        plt.tight_layout()
        plt.savefig(GRAPHS_REFILL_DIR / "threshold_refill_timeline.png", dpi=150)
        plt.close()

    system_log(f"Grafici generati in: {GRAPHS_BASE_DIR}")


############################################
# ESPERIMENTO
############################################

def cleanup_all() -> None:
    system_log("Pulizia ambiente")
    cleanup_swarm_pool()
    stop_server_baremetal(None)


def run_experiment() -> tuple[int, int]:
    reset_local_output_dirs()
    ensure_local_artifacts()
    ensure_remote_layout()

    server_pid: str | None = None
    ok_messages: list[int] = []
    error_messages: list[int] = []
    latencies_ms: list[float] = []
    refill_events: list[tuple[int, int, int, str]] = []
    refill_stop = threading.Event()
    refill_thread: threading.Thread | None = None

    try:
        cleanup_all()

        server_pid = start_server_baremetal()
        start_middlebox_swarm_pool()

        refill_thread = threading.Thread(
            target=refill_manager,
            args=(refill_stop, refill_events),
            daemon=True,
        )
        refill_thread.start()

        for index in range(1, N_REQUESTS + 1):
            system_log(f"Invio richiesta {index}/{N_REQUESTS} da client baremetal")
            result = send_client_request(index)
            append_experiment_log(result)
            latencies_ms.append((result.completed_ns - result.started_ns) / 1_000_000.0)

            if result.ok:
                ok_messages.append(index)
                system_log(f"Richiesta {index}: OK")
            else:
                error_messages.append(index)
                system_log(f"Richiesta {index}: ERRORE")

            if index < N_REQUESTS and REQUEST_RATE_MS > 0:
                time.sleep(REQUEST_RATE_MS / 1000.0)

        sync_server_log()
        write_summary(ok_messages, error_messages)
        generate_graphs(latencies_ms, refill_events, len(ok_messages), len(error_messages))

        if error_messages:
            append_local(MIDDLEBOX_RUNTIME_LOG, "\n===== DIAGNOSTICA MIDDLEBOX =====\n")
            append_local(MIDDLEBOX_RUNTIME_LOG, collect_middlebox_diagnostics())

        return len(ok_messages), len(error_messages)

    finally:
        refill_stop.set()
        if refill_thread is not None:
            refill_thread.join(timeout=2.0)
        if server_pid:
            stop_server_baremetal(server_pid)
        cleanup_swarm_pool()


############################################
# CLI
############################################

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Misure orchestrate: client/server baremetal via SSH, middlebox Docker Swarm."
    )
    parser.add_argument("--requests", type=int, default=N_REQUESTS)
    parser.add_argument("--type", choices=["GET", "POST", "get", "post"], default=REQUEST_TYPE)
    parser.add_argument("--request-rate-ms", type=int, default=REQUEST_RATE_MS)
    parser.add_argument("--initial-operators", type=int, default=INITIAL_OPERATORS)
    parser.add_argument("--min-ready", type=int, default=MIN_READY_OPERATORS)
    parser.add_argument("--scale-up-by", type=int, default=SCALE_UP_BY)
    parser.add_argument("--autoscale-rate-ms", type=int, default=AUTOSCALE_RATE_MS)
    return parser.parse_args()


def main() -> None:
    global N_REQUESTS, REQUEST_TYPE, REQUEST_RATE_MS
    global INITIAL_OPERATORS, MIN_READY_OPERATORS, SCALE_UP_BY, AUTOSCALE_RATE_MS

    args = parse_args()
    if args.requests <= 0:
        raise ValueError("--requests deve essere > 0")
    if args.request_rate_ms < 0 or args.autoscale_rate_ms < 0:
        raise ValueError("I rate devono essere >= 0")

    N_REQUESTS = args.requests
    REQUEST_TYPE = args.type.upper()
    REQUEST_RATE_MS = args.request_rate_ms
    INITIAL_OPERATORS = args.initial_operators
    MIN_READY_OPERATORS = args.min_ready
    SCALE_UP_BY = args.scale_up_by
    AUTOSCALE_RATE_MS = args.autoscale_rate_ms

    system_log("================ START MisureOrchestrate ================")
    system_log(f"N_MESSAGES={N_REQUESTS} CLIENT_ID={CLIENT_ID}")
    system_log(f"Gateway URL=https://{GATEWAY_HOST_FOR_CLIENT}:{GATEWAY_PORT}/function/init")
    system_log(f"REQUEST_RATE_MS={REQUEST_RATE_MS}")
    system_log("Client/server baremetal; middlebox Docker Swarm; operator single-use.")

    ok = 0
    errors = 0
    try:
        ok, errors = run_experiment()
    except Exception as exc:
        system_log(f"ERRORE durante run_experiment: {exc}")
        try:
            append_local(MIDDLEBOX_RUNTIME_LOG, "\n===== DIAGNOSTICA POST-ERRORE =====\n")
            append_local(MIDDLEBOX_RUNTIME_LOG, collect_middlebox_diagnostics())
        except Exception:
            pass
        raise
    finally:
        try:
            cleanup_all()
        except Exception as cleanup_exc:
            system_log(f"Errore durante cleanup finale: {cleanup_exc}")

    print("MisureOrchestrate completato")
    print(f"Totale OK: {ok}")
    print(f"Totale ERRORI: {errors}")
    print(f"Runtime logs: {RUNTIME_DIR}")
    print(f"Summary: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
