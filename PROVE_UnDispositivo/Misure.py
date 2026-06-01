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

N = 5
TYPE = "GET"
OPS = [1, 2, 3, 4]

NETWORK = "tlmsp-net"

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

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"

SERVER_CERT_HOST_PATH = SERVER_CERTS_HOST_DIR / "cert.pem"
SERVER_KEY_HOST_PATH = SERVER_CERTS_HOST_DIR / "key.pem"

CLIENT_BINARY = MB_DIR / "client"
MIDDLEBOX_BINARY = MB_DIR / "middlebox"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"

TOKEN = "token"
DIRECT_SERVER_TARGET = "localhost:8000"

# Cartella base storica; ogni run usa una sottocartella timestampata.
BASE_OUTPUT_DIR = PROJECT_ROOT / "Misure"


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
MIDDLEBOX_CONTAINER = "middlebox"
MIDDLEBOX_IMAGE = "middlebox"

STARTUP_TIMEOUT = 60
SETTLE_TIMEOUT = 10
SETTLE_INTERVAL = 0.2
SETTLE_STABLE_FOR = 0.5

_RUN_ENV_CACHE: dict[str, str] | None = None
_SERVER_PROCESS: subprocess.Popen[str] | None = None

TIMESTAMP_PATTERN = re.compile(r"\bt(\d+)\b\s*:?\s*[^\n\r]*?=\s*(\d+)")

EXPECTED_TIMESTAMPS = {
    "Client": {
        1: {1, 10, 20, 21, 22, 23, 24, 27, 28},
        2: {1, 10, 20, 21, 22, 23, 24, 27, 28},
        3: {1, 10, 20, 21, 22, 23, 24, 27, 28},
        4: {1, 10, 20, 21, 22, 23, 24, 27, 28},
    },
    "Middlebox": {
        1: {2, 3, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 37, 38, 39, 40},
        2: {2, 3, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 37, 38, 39, 40},
        3: {2, 3, 37, 38, 39, 40},
        4: set(),
    },
    "Server": {
        1: {4, 5, 6, 7, 25, 26},
        2: {4, 5, 6, 7, 25, 26},
        3: set(),
        4: {25, 26},
    },
}

REQUIRED_TIMESTAMPS = {
    "Client": {1: {10}, 2: {10}, 3: {10}, 4: {10}},
    "Middlebox": {1: {2, 3, 40}, 2: {2, 3, 40}, 3: {2, 3, 40}, 4: set()},
    "Server": {1: {25, 26}, 2: {25, 26}, 3: set(), 4: {25, 26}},
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


def ensure_go_binaries() -> None:
    if not GO_PROFESSOR.exists():
        raise FileNotFoundError(f"Compilatore Go del professore non trovato: {GO_PROFESSOR}")

    sources = [MB_DIR / "client.go", MB_DIR / "middlebox.go", MB_DIR / "middleboxHandler.go", MB_DIR / "messageTypes.go"]
    if any(not src.exists() for src in sources):
        missing = ", ".join(str(src) for src in sources if not src.exists())
        raise FileNotFoundError(f"Sorgenti Go mancanti: {missing}")

    client_needs_build = (not CLIENT_BINARY.exists()) or (
        CLIENT_BINARY.stat().st_mtime < (MB_DIR / "client.go").stat().st_mtime
    )
    if client_needs_build:
        result = subprocess.run(
            [str(GO_PROFESSOR), "build", "-o", str(CLIENT_BINARY), "client.go"],
            cwd=str(MB_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Impossibile compilare client con Go professore:\n{combine_output(result)}")

    mb_inputs_mtime = max((MB_DIR / "middlebox.go").stat().st_mtime, (MB_DIR / "middleboxHandler.go").stat().st_mtime, (MB_DIR / "messageTypes.go").stat().st_mtime)
    middlebox_needs_build = (not MIDDLEBOX_BINARY.exists()) or (MIDDLEBOX_BINARY.stat().st_mtime < mb_inputs_mtime)
    if middlebox_needs_build:
        result = subprocess.run(
            [
                str(GO_PROFESSOR),
                "build",
                "-o",
                str(MIDDLEBOX_BINARY),
                "middlebox.go",
                "middleboxHandler.go",
                "messageTypes.go",
            ],
            cwd=str(MB_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Impossibile compilare middlebox con Go professore:\n{combine_output(result)}")


def wait_for_marker(log_path: Path, marker: str, start_index: int = 0) -> None:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if marker in read_text_from_offset(log_path, start_index):
            return
        time.sleep(0.5)
    raise TimeoutError(f"Marker non trovato nel log {log_path}: {marker}")


def wait_for_any_marker(log_path: Path, markers: list[str], start_index: int = 0) -> str:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        chunk = read_text_from_offset(log_path, start_index)
        for marker in markers:
            if marker in chunk:
                return marker
        time.sleep(0.5)
    joined = " | ".join(markers)
    raise TimeoutError(f"Nessuno dei marker trovati nel log {log_path}: {joined}")


def wait_until_log_settles(paths: list[Path]) -> None:
    deadline = time.time() + SETTLE_TIMEOUT
    last_sizes: dict[Path, int] = {}
    stable_since: float | None = None

    while time.time() < deadline:
        current_sizes = {path: path.stat().st_size if path.exists() else 0 for path in paths}

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


def write_operation_marker(exp: int, op: int) -> dict[str, int]:
    marker = f"--- OPERAZIONE {op} (esperimento {exp}) ---\n"
    append_text(CLIENT_RUNTIME_LOG, marker)
    append_text(MIDDLEBOX_RUNTIME_LOG, marker)
    append_text(SERVER_RUNTIME_LOG, marker)
    return {
        "Client": file_size(CLIENT_RUNTIME_LOG),
        "Middlebox": file_size(MIDDLEBOX_RUNTIME_LOG),
        "Server": file_size(SERVER_RUNTIME_LOG),
    }


def append_container_timing(container_name: str, phase: str, timestamp_ns: int) -> None:
    append_text(CONTAINER_TIMES_LOG, f"{container_name} {phase} = {timestamp_ns} ns\n")


def wait_for_expected_timestamps(op: int, start_offsets: dict[str, int]) -> None:
    component_logs = {
        "Client": CLIENT_RUNTIME_LOG,
        "Middlebox": MIDDLEBOX_RUNTIME_LOG,
        "Server": SERVER_RUNTIME_LOG,
    }

    deadline = time.time() + STARTUP_TIMEOUT
    missing: dict[str, set[int]] = {}
    while time.time() < deadline:
        missing = {}
        for component, path in component_logs.items():
            expected = REQUIRED_TIMESTAMPS[component].get(op, set())
            if not expected:
                continue
            start = start_offsets.get(component, 0)
            seen = {ts for ts, _ in parse_ordered_times(read_text_from_offset(path, start))}
            not_seen = expected - seen
            if not_seen:
                missing[component] = not_seen

        if not missing:
            return
        time.sleep(0.1)

    details = "; ".join(f"{component}: missing {sorted(values)}" for component, values in missing.items())
    raise TimeoutError(
        f"Timestamp attesi non trovati per operazione {op} entro timeout: {details}"
    )


############################################
# DOCKER MIDDLEBOX
############################################


def cleanup_containers() -> None:
    run(f"docker rm -f {quote(MIDDLEBOX_CONTAINER)}", check=False)


def build_images() -> None:
    print(f"!!! BUILD MIDDLEBOX CONTAINER - START : {time.time_ns()}")
    run("docker build -f DC/Middlebox/Dockerfile.middlebox -t middlebox .", cwd=PROJECT_ROOT)
    print(f"!!! BUILD MIDDLEBOX CONTAINER - END : {time.time_ns()}")


def create_middlebox_container(exp: int) -> None:
    start_ns = time.time_ns()
    print(f"!!! RUN CONTAINER {MIDDLEBOX_CONTAINER} - START : {start_ns}")
    append_container_timing(MIDDLEBOX_CONTAINER, "START", start_ns)

    run(
        " ".join(
            [
                "docker run -d",
                f"--name {quote(MIDDLEBOX_CONTAINER)}",
                f"--network {quote(NETWORK)}",
                "-p 127.0.0.1:8443:8443",
                "--add-host server:host-gateway",
                f"-v {quote(str(BASE_DIR))}:{quote(SHARED_VOLUME)}",
                quote(MIDDLEBOX_IMAGE),
                "tail -f /dev/null",
            ]
        )
    )

    run(f"docker cp {quote(str(MIDDLEBOX_BINARY))} {quote(MIDDLEBOX_CONTAINER)}:/app/middlebox")
    run(f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote('chmod +x /app/middlebox')}")

    end_ns = time.time_ns()
    print(f"!!! RUN CONTAINER {MIDDLEBOX_CONTAINER} - END : {end_ns}")
    append_container_timing(MIDDLEBOX_CONTAINER, "END", end_ns)
    append_text(MIDDLEBOX_RUNTIME_LOG, f"[MB] Container creation time = {end_ns - start_ns} ns\n")


def start_middlebox() -> None:
    start_idx = file_size(MIDDLEBOX_RUNTIME_LOG)
    cmd = f"cd /app && ./middlebox >> {quote(container_path(MIDDLEBOX_RUNTIME_LOG))} 2>&1"
    start_ns = time.time_ns()
    run(f"docker exec -d {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote(cmd)}")
    wait_for_any_marker(
        MIDDLEBOX_RUNTIME_LOG,
        [
            "[MB] Middlebox TLS server listening on :8443",
            "[MB] Middlebox listening on :8443",
        ],
        start_idx,
    )
    end_ns = time.time_ns()
    append_text(MIDDLEBOX_RUNTIME_LOG, f"[MB] Service startup time = {end_ns - start_ns} ns\n")


def stop_middlebox() -> None:
    kill_command = (
        "pkill -INT -f middlebox 2>/dev/null || true; "
        "sleep 0.5; "
        "pkill -TERM -f middlebox 2>/dev/null || true; "
        "sleep 0.5; "
        "pkill -KILL -f middlebox 2>/dev/null || true"
    )
    run(f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote(kill_command)}", check=False)


def reset_middlebox_delegation() -> None:
    run(
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc "
        f"{quote('pkill -USR1 -f middlebox 2>/dev/null || true')}",
        check=False,
    )
    time.sleep(0.1)
    run(
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc "
        f"{quote('rm -f /certs/dc.cred /certs/dckey.pem 2>/dev/null; true')}",
        check=False,
    )
    verify_cmd = (
        "if [ -f /certs/dc.cred ] || [ -f /certs/dckey.pem ]; then "
        "echo '[MB] Deleghe non eliminate'; exit 1; "
        "fi"
    )
    run(f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote(verify_cmd)}", check=True)


############################################
# SERVER/CLIENT BAREMETAL
############################################


def start_server() -> None:
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


def stop_server() -> None:
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


def client_request(target: str) -> str:
    url = f"https://{target}/function/init"
    server_name = "server"
    if target.endswith(":8000"):
        server_name = "localhost"

    cmd = [
        str(CLIENT_BINARY),
        "-ca",
        str(CLIENT_CA_HOST_PATH),
        "-servername",
        server_name,
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


def wait_logs_after_operation(op: int, start_offsets: dict[str, int]) -> None:
    wait_for_expected_timestamps(op, start_offsets)
    wait_until_log_settles([SERVER_RUNTIME_LOG, MIDDLEBOX_RUNTIME_LOG, CLIENT_RUNTIME_LOG])
    time.sleep(0.2)


############################################
# ANALISI
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


def latest_times_per_operation(text: str, allowed: set[int]) -> dict[int, dict[int, int]]:
    operations: dict[int, dict[int, int]] = {1: {}, 2: {}, 3: {}, 4: {}}
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
    reconstructed: dict[int, dict[int, dict[int, int]]] = {
        exp: {1: {}, 2: {}, 3: {}, 4: {}} for exp in all_exp
    }

    client_allowed = set().union(*EXPECTED_TIMESTAMPS["Client"].values())
    middlebox_allowed = set().union(*EXPECTED_TIMESTAMPS["Middlebox"].values())
    server_allowed = set().union(*EXPECTED_TIMESTAMPS["Server"].values())

    for exp in all_exp:
        client_ops = latest_times_per_operation(client_sections.get(exp, ""), client_allowed)
        middlebox_ops = latest_times_per_operation(middlebox_sections.get(exp, ""), middlebox_allowed)
        server_ops = latest_times_per_operation(server_sections.get(exp, ""), server_allowed)

        for op in range(1, 5):
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

    averages: dict[int, dict[str, defaultdict[int | str, list[int]]]] = {
        op: {"times": defaultdict(list), "diffs": defaultdict(list)} for op in range(1, 5)
    }
    graph_points: dict[int, dict[str, list[tuple[int, int]]]] = {
        op: {"t1": [], "t10": [], "t10 - t1": []} for op in range(1, 5)
    }

    with results_path.open("w", encoding="utf-8") as results_file:
        for exp in sorted(operations):
            for op in range(1, 5):
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

        diff_order = [
            "t2 - t1", "t28 - t27", "t3 - t2", "t4 - t3", "t38 - t37", "t39 - t38", "t40 - t39",
            "t26 - t25", "t5 - t4", "t6 - t5", "t7 - t6", "t8 - t7", "t11 - t8", "t12 - t11",
            "t13 - t12", "t14 - t13", "t15 - t14", "t16 - t15", "t17 - t16", "t18 - t17",
            "t19 - t18", "t9 - t19", "t9 - t8", "t10 - t9", "t10 - t40", "t10 - t1",
            "t21 - t20", "t23 - t22",
        ]
        for op in range(1, 5):
            results_file.write(f"AVERAGE operazione_N_{op}:\n")
            for key in sorted(averages[op]["times"]):
                results_file.write(f"t{key} average = {round(statistics.fmean(averages[op]['times'][key]))} ns\n")
            for label in diff_order:
                values = averages[op]["diffs"].get(label, [])
                if values:
                    results_file.write(f"{label} average = {round(statistics.fmean(values))} ns\n")
            results_file.write("--------------------\n")

    with graphs_path.open("w", encoding="utf-8") as graphs_file:
        for metric in ("t1", "t10", "t10 - t1"):
            for op in range(1, 5):
                graphs_file.write(f"SERIE {metric} operazione_N_{op}:\n")
                for exp, value in graph_points[op][metric]:
                    graphs_file.write(f"{series_label(exp, op)} = {value}\n")
                graphs_file.write("--------------------\n")


############################################
# ESPERIMENTI
############################################


def stop_experiment_services() -> None:
    stop_middlebox()
    stop_server()
    time.sleep(1)


def run_single_experiment(exp: int) -> None:
    print(f"\n===== ESPERIMENTO {exp} =====\n")
    write_experiment_markers(exp)

    cleanup_containers()
    create_middlebox_container(exp)

    try:
        start_server()
        start_middlebox()

        if 1 in OPS:
            print("OPERAZIONE 1")
            offsets = write_operation_marker(exp, 1)
            client_request("127.0.0.1:8443")
            wait_logs_after_operation(1, offsets)

        if 2 in OPS:
            print("OPERAZIONE 2")
            offsets = write_operation_marker(exp, 2)
            reset_middlebox_delegation()
            client_request("127.0.0.1:8443")
            wait_logs_after_operation(2, offsets)

        if 3 in OPS:
            print("OPERAZIONE 3")
            offsets = write_operation_marker(exp, 3)
            client_request("127.0.0.1:8443")
            wait_logs_after_operation(3, offsets)

        if 4 in OPS:
            print("OPERAZIONE 4")
            offsets = write_operation_marker(exp, 4)
            client_request(DIRECT_SERVER_TARGET)
            wait_logs_after_operation(4, offsets)
    finally:
        stop_experiment_services()
        cleanup_containers()


def run_experiments() -> None:
    reset_output_directories()
    ensure_directories()
    ensure_network()
    ensure_external_certificates()
    ensure_go_binaries()
    cleanup_containers()
    build_images()

    for exp in range(1, N + 1):
        run_single_experiment(exp)


def emergency_cleanup_docker() -> None:
    for port in (5000, 8000, 8443):
        try:
            subprocess.run(f"fuser -k {port}/tcp 2>/dev/null", shell=True, check=False)
        except Exception:
            pass

    try:
        run(f"docker rm -f {quote(MIDDLEBOX_CONTAINER)}", check=False)
    except Exception:
        pass

    stop_server()


def verify_cleanup_docker() -> bool:
    all_clear = True
    for port in (5000, 8000, 8443):
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
        print("\nEsperimenti completati")
    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            emergency_cleanup_docker()
            verify_cleanup_docker()
        except Exception as exc:
            print(f"Errore durante emergency_cleanup_docker: {exc}")


if __name__ == "__main__":
    main()
