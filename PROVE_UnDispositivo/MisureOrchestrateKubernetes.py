from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shlex
import shutil
import statistics
import subprocess
import threading
import time
from enum import IntEnum
from datetime import datetime
from pathlib import Path

N_REQUESTS = 5
REQUEST_TYPE = "GET"
REQUEST_RATE_MS = 10000
INITIAL_OPERATORS = 15
MIN_READY_OPERATORS = 10
SCALE_UP_BY = 10
MAX_OPERATORS = 60
AUTOSCALE_RATE_MS = 3000
SGX_REFILL_COOLDOWN_S = 2
NOSGX_REFILL_COOLDOWN_S = 0
TOKEN = "token"
CLIENT_ID = "client"
ALWAYS_REBUILD_IMAGES = False #or True
OPERATOR_MODE = "nosgx" # "sgx" per modalità con SGX passthrough, "nosgx" per modalità senza SGX (stesso flusso gateway->operator single-use, senza SGX)
REFILL_COOLDOWN_OVERRIDE_S: int | None = None
REFILL_TARGET_READY_OVERRIDE: int | None = None

OPERATOR_DEPLOYMENT = "middlebox-operator-kubernetes"
GATEWAY_DEPLOYMENT = "middlebox-gateway-kubernetes"
OPERATOR_LABEL = "app=middlebox-operator-kubernetes"

# Docker configuration (client/server esterni)
DOCKER_NETWORK_NAME = "misure-kubernetes"
SERVER_CONTAINER_NAME = "server-docker-external"
SERVER_IMAGE = "server:latest"
CLIENT_IMAGE = "client:latest"
SERVER_DOCKER_PORT_CERTS = 5000
SERVER_DOCKER_PORT_APP = 8000

SCRIPT_DIR = Path(__file__).resolve().parent


def resolve_project_root(start_dir: Path) -> Path:
    for candidate in [start_dir, *start_dir.parents]:
        if (candidate / "PerformanceMeasuring").exists() and (candidate / "DC/Middlebox").exists():
            return candidate
    raise FileNotFoundError(
        "Impossibile risolvere PROJECT_ROOT: servono le directory PerformanceMeasuring e DC/Middlebox"
    )


PROJECT_ROOT = resolve_project_root(SCRIPT_DIR)
MB_DIR = PROJECT_ROOT / "DC/Middlebox"
SERVER_DIR = PROJECT_ROOT / "PerformanceMeasuring"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"
GO_PROFESSOR = PROJECT_ROOT / "DC" / "go" / "bin" / "go"
SERVER_CERTS_HOST_DIR = PROJECT_ROOT / "certs_external" / "server"

# Cartella base storica; ogni run usa una sottocartella timestampata.
BASE_OUTPUT_DIR = PROJECT_ROOT / "MisureOrchestrateKubernetes"


def _run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


BASE_DIR = BASE_OUTPUT_DIR / _run_timestamp()
ANALYSIS_DIR = BASE_DIR / "Analisi"
RUNTIME_DIR = BASE_DIR / "Runtime"
ORCHESTRATE_LOG_DIR = BASE_DIR / "Orchestrate"
LOCAL_GENERATE_TOOL = RUNTIME_DIR / "generate_dc_tool"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"
SUMMARY_FILE = ANALYSIS_DIR / "OrchestrateKubernetesSummary.txt"

CLIENT_TXT_LOG = ORCHESTRATE_LOG_DIR / "ClientLog.txt"
MIDDLEBOX_TXT_LOG = ORCHESTRATE_LOG_DIR / "MiddleboxLog.txt"


class LogLevel(IntEnum):
    QUIET = 0
    INFO = 1
    DEBUG = 2


# Output minimale: bootstrap pool, invio/esito richieste, refill.
CONSOLE_LOG_LEVEL = LogLevel.QUIET
_NODE_HOST_CACHE: str | None = None


def get_orchestrate_manifest(operator_mode: str) -> Path:
    if operator_mode == "nosgx":
        return MB_DIR / "orchestrate_kubernetes_no_sgx.yaml"
    return MB_DIR / "orchestrate_kubernetes.yaml"


def quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def system_log(message: str, level: LogLevel = LogLevel.INFO) -> None:
    if level <= CONSOLE_LOG_LEVEL:
        print(f"[{now_stamp()}] [SYSTEM] {message}")


def run(command: str, *, cwd: Path | None = None, check: bool = True, timeout: int | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_output = (exc.stdout or "") + (exc.stderr or "")
        if check:
            raise RuntimeError(
                f"Comando in timeout ({timeout}s): {command}\n{timeout_output}"
            ) from exc
        return timeout_output + f"\n[timeout after {timeout}s]"
    output = (result.stdout or "") + (result.stderr or "")
    if check and result.returncode != 0:
        raise RuntimeError(f"Comando fallito ({result.returncode}): {command}\n{output}")
    return output


def docker_image_exists(image_name: str) -> bool:
    result = subprocess.run(
        f"docker image inspect {quote(image_name)}",
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def docker_image_startup_signature(image_name: str) -> str:
    result = subprocess.run(
        f"docker image inspect {quote(image_name)}",
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""

    try:
        parsed = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ""

    if not isinstance(parsed, list) or not parsed:
        return ""

    config = (parsed[0] or {}).get("Config") or {}
    entrypoint = config.get("Entrypoint") or []
    cmd = config.get("Cmd") or []

    parts: list[str] = []
    if isinstance(entrypoint, list):
        parts.extend(str(x) for x in entrypoint)
    elif isinstance(entrypoint, str):
        parts.append(entrypoint)

    if isinstance(cmd, list):
        parts.extend(str(x) for x in cmd)
    elif isinstance(cmd, str):
        parts.append(cmd)

    return " ".join(parts).strip().lower()


def needs_nosgx_operator_image_rebuild(image_name: str) -> tuple[bool, str]:
    signature = docker_image_startup_signature(image_name)
    if not signature:
        return True, "firma avvio non rilevabile"
    if "gramine" in signature or "sgx" in signature:
        return True, f"firma avvio incompatibile con no-SGX: {signature}"
    if "operator" not in signature:
        return True, f"entrypoint/cmd non contiene operator: {signature}"
    return False, signature


def ensure_server_generate_tool() -> None:
	"""Compila il tool per la generazione di delegated credentials dal sorgente Go."""
	dc_source = PROJECT_ROOT / "DC" / "go" / "src" / "crypto" / "tls" / "generate_delegated_credential.go"
	if not GO_PROFESSOR.exists():
		raise FileNotFoundError(f"Compilatore Go non trovato: {GO_PROFESSOR}")
	if not dc_source.exists():
		raise FileNotFoundError(f"Sorgente tool generate DC non trovato: {dc_source}")
	run(
		f"{quote(GO_PROFESSOR)} build -o {quote(LOCAL_GENERATE_TOOL)} {quote(dc_source)}",
	)
	system_log(f"Tool generate_dc_tool compilato: {LOCAL_GENERATE_TOOL}")


def ensure_directories(reset: bool) -> None:
    if reset:
        for directory in (ANALYSIS_DIR, RUNTIME_DIR, ORCHESTRATE_LOG_DIR):
            if directory.exists():
                shutil.rmtree(directory)
    for directory in (BASE_OUTPUT_DIR, BASE_DIR, ANALYSIS_DIR, RUNTIME_DIR, ORCHESTRATE_LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def parse_client_timestamps(output: str) -> tuple[int | None, int | None]:
    t1: int | None = None
    t10: int | None = None
    for line in output.splitlines():
        if "t1:" in line and "=" in line:
            try:
                t1 = int(line.split("=")[-1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        if "t10:" in line and "=" in line:
            try:
                t10 = int(line.split("=")[-1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return t1, t10


def parse_timestamp_series(text: str, tag: str) -> list[int]:
    pattern = re.compile(rf"\b{re.escape(tag)}:\s*\[[^\]]+\].*?=\s*(\d+)\s*ns")
    return [int(match.group(1)) for match in pattern.finditer(text)]


def paired_deltas(start_series: list[int], end_series: list[int]) -> list[int]:
    deltas: list[int] = []
    end_idx = 0
    for start_value in start_series:
        while end_idx < len(end_series) and end_series[end_idx] < start_value:
            end_idx += 1
        if end_idx >= len(end_series):
            break
        deltas.append(end_series[end_idx] - start_value)
        end_idx += 1
    return deltas


def compute_breakdown_metrics() -> dict[str, list[int]]:
    def read_text(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    client_text = read_text(CLIENT_RUNTIME_LOG)
    middlebox_text = read_text(MIDDLEBOX_RUNTIME_LOG)

    t1_client = parse_timestamp_series(client_text, "t1")
    t25_gateway = parse_timestamp_series(middlebox_text, "t25")
    t27_gateway = parse_timestamp_series(middlebox_text, "t27")
    t28_gateway = parse_timestamp_series(middlebox_text, "t28")
    t29_operator = parse_timestamp_series(middlebox_text, "t29")
    t37_xcode = parse_timestamp_series(middlebox_text, "t37")
    t38_xcode = parse_timestamp_series(middlebox_text, "t38")
    t3_operator = parse_timestamp_series(middlebox_text, "t3")
    t4_server = parse_timestamp_series(middlebox_text, "t4")
    t5_server = parse_timestamp_series(middlebox_text, "t5")
    t6_server = parse_timestamp_series(middlebox_text, "t6")

    return {
        "client_to_gateway_latency_ns": paired_deltas(t1_client, t25_gateway),
        "gateway_selection_to_forward_ns": paired_deltas(t27_gateway, t28_gateway),
        "gateway_to_operator_delivery_ns": paired_deltas(t28_gateway, t29_operator),
        "xcode_validation_ns": paired_deltas(t37_xcode, t38_xcode),
        "operator_to_server_latency_ns": paired_deltas(t3_operator, t4_server),
        "server_cert_generation_ns": paired_deltas(t5_server, t6_server),
    }


def write_breakdown_block(handle, label: str, values: list[int]) -> None:
    handle.write(f"{label}: campioni={len(values)}\n")
    if not values:
        handle.write("  n/d\n")
        return
    handle.write(f"  media={round(statistics.fmean(values))} ns\n")
    handle.write(f"  mediana={round(statistics.median(values))} ns\n")
    handle.write(f"  min={min(values)} ns\n")
    handle.write(f"  max={max(values)} ns\n")


def build_images(force_rebuild: bool, operator_mode: str) -> None:
    middlebox_image = "middleboxsgxshield:latest"
    middlebox_bin = MB_DIR / "middleboxsgx"

    cfgo_candidates = [
        Path.home() / "go_DC/bin/go",
        Path.home() / "go/bin/go",
        MB_DIR / "go/bin/go",
    ]
    cfgo_bin: Path | None = next((p for p in cfgo_candidates if p.is_file()), None)

    if not force_rebuild and docker_image_exists(middlebox_image):
        system_log("Immagine middleboxsgxshield:latest presente, skip build")
    else:
        if cfgo_bin is not None:
            system_log(f"Compilo middleboxsgx con cfgo: {cfgo_bin}")
            run(
                (
                    f"GOWORK=off GO111MODULE=on GOOS=linux GOARCH=amd64 {quote(cfgo_bin)} "
                    "build -o middleboxsgx middleboxsgx.go middleboxHandler.go messageTypes.go"
                ),
                cwd=MB_DIR,
                check=True,
            )
        else:
            if not middlebox_bin.exists():
                raise RuntimeError(
                    "Nessun compilatore cfgo trovato (~/go_DC/bin/go, ~/go/bin/go o DC/Middlebox/go/bin/go) "
                    "e binary middleboxsgx mancante."
                )
            system_log(
                "Nessun cfgo trovato: uso middleboxsgx gia presente su disco per rebuild immagine"
            )

        system_log("Build immagine middleboxsgxshield:latest")
        run(
            "docker build -f middleboxsgxshield.Dockerfile -t middleboxsgxshield:latest .",
            cwd=MB_DIR,
            check=True,
        )

    build_steps = [
        (
            "mb_gateway",
            "docker build -f Dockerfile.gateway -t mb_gateway:latest .",
            MB_DIR,
        ),
        (
            "mb_operator",
            "docker build -f Dockerfile.operator -t mb_operator:latest .",
            MB_DIR,
        ),
        (
            "client",
            "docker build -f Dockerfile.client -t client:latest .",
            MB_DIR,
        ),
        (
            "server",
            "docker build -f Dockerfile.server -t server:latest .",
            SERVER_DIR,
        ),
    ]

    for image, cmd, cwd in build_steps:
        image_tag = image + ":latest"
        if not force_rebuild and docker_image_exists(image_tag):
            if image == "mb_operator" and operator_mode == "nosgx":
                must_rebuild, reason = needs_nosgx_operator_image_rebuild(image_tag)
                if must_rebuild:
                    system_log(
                        "Immagine mb_operator:latest non valida per no-SGX "
                        f"({reason}), rebuild forzato"
                    )
                else:
                    system_log(
                        f"Immagine {image}:latest presente e valida per no-SGX (startup: {reason}), skip build"
                    )
                    continue
            else:
                system_log(f"Immagine {image}:latest presente, skip build")
                continue
        system_log(f"Build immagine {image}:latest")
        run(cmd, cwd=cwd, check=True)


def gather_rollout_diagnostics(namespace: str, deployment_name: str) -> str:
    lines: list[str] = []
    selector = f"app={deployment_name}"
    lines.append(f"=== Diagnostica rollout deployment/{deployment_name} ===")

    lines.append("--- deployment status ---")
    lines.append(
        run(
            f"kubectl -n {quote(namespace)} get deployment {quote(deployment_name)} -o wide",
            check=False,
        ).strip()
    )

    lines.append("--- describe deployment ---")
    lines.append(
        run(
            f"kubectl -n {quote(namespace)} describe deployment {quote(deployment_name)}",
            check=False,
        ).strip()
    )

    pod_names_out = run(
        (
            f"kubectl -n {quote(namespace)} get pods -l {quote(selector)} "
            "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}'"
        ),
        check=False,
    ).strip().strip("'")
    pod_names = [p for p in pod_names_out.splitlines() if p.strip()]

    lines.append("--- pods ---")
    lines.append(
        run(
            f"kubectl -n {quote(namespace)} get pods -l {quote(selector)} -o wide",
            check=False,
        ).strip()
    )

    for pod_name in pod_names[:3]:
        lines.append(f"--- describe pod/{pod_name} ---")
        lines.append(
            run(
                f"kubectl -n {quote(namespace)} describe pod {quote(pod_name)}",
                check=False,
            ).strip()
        )
        lines.append(f"--- logs pod/{pod_name} (tail 120) ---")
        lines.append(
            run(
                f"kubectl -n {quote(namespace)} logs {quote(pod_name)} --tail=120",
                check=False,
            ).strip()
        )
        lines.append(f"--- logs --previous pod/{pod_name} (tail 120) ---")
        lines.append(
            run(
                f"kubectl -n {quote(namespace)} logs {quote(pod_name)} --previous --tail=120",
                check=False,
            ).strip()
        )

    lines.append("--- eventi recenti namespace ---")
    lines.append(
        run(
            f"kubectl -n {quote(namespace)} get events --sort-by=.lastTimestamp | tail -n 60",
            check=False,
        ).strip()
    )

    return "\n".join(line for line in lines if line)


def maybe_load_kind_images(kind_cluster_name: str, operator_mode: str) -> None:
    context = run("kubectl config current-context", check=False).strip()
    if not context.startswith("kind-"):
        system_log("Context non-kind: skip kind load")
        return

    if shutil.which("kind") is None:
        raise RuntimeError(
            "Context kind rilevato ma comando 'kind' non trovato nel PATH. "
            "Installa kind oppure avvia con --skip-kind-load."
        )

    images = ["mb_gateway:latest", "server:latest"]
    if operator_mode == "nosgx":
        images.append("mb_operator:latest")
    else:
        images.append("middleboxsgxshield:latest")

    system_log(
        "Context kind rilevato: carico immagini nel cluster "
        f"(operator-mode={operator_mode}, immagini={', '.join(images)})"
    )
    load_start_ns = time.time_ns()
    for image in images:
        run(f"kind load docker-image {quote(image)} --name {quote(kind_cluster_name)}", check=True)
    load_elapsed_ns = time.time_ns() - load_start_ns
    system_log(
        f"kind load completato in {load_elapsed_ns} ns ({load_elapsed_ns / 1_000_000_000.0:.3f} s)"
    )


def ensure_kubernetes_cluster_ready(namespace: str) -> None:
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
            "Imposta un contesto prima di eseguire l'orchestrazione.\n"
            f"Context disponibili: {available}\n"
            "Esempi:\n"
            "  kubectl config use-context <nome-contesto>\n"
            "  kubectl config current-context"
        )

    api_probe = subprocess.run(
        "kubectl --request-timeout=5s get --raw=/readyz",
        shell=True,
        capture_output=True,
        text=True,
    )
    api_output = ((api_probe.stdout or "") + (api_probe.stderr or "")).strip()
    if api_probe.returncode != 0 or "ok" not in (api_probe.stdout or "").lower():
        raise RuntimeError(
            "API server Kubernetes non raggiungibile per il contesto corrente.\n"
            f"Contesto: {current_context}\n"
            f"Namespace target: {namespace}\n"
            f"Dettagli: {api_output}\n"
            "Verifica con:\n"
            "  kubectl cluster-info\n"
            "  kubectl get nodes"
        )


def deploy_stack(namespace: str, operator_mode: str) -> None:
    manifest = get_orchestrate_manifest(operator_mode)
    system_log(f"Deploy manifest: {manifest.name} (operator-mode={operator_mode})")
    run(f"kubectl -n {quote(namespace)} apply -f {quote(manifest)}", check=True)

    # Il server certs deve essere raggiungibile su 5000 anche prima che la porta
    # TLS 8000 sia pronta, altrimenti il service resta senza endpoint e l'operator
    # fallisce la richiesta /certs con connection refused.
    run(
        (
            f"kubectl -n {quote(namespace)} patch deployment server-kubernetes --type=json -p "
            "'["
            "{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/readinessProbe/tcpSocket/port\",\"value\":5000},"
            "{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/startupProbe/tcpSocket/port\",\"value\":5000},"
            "{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/livenessProbe/tcpSocket/port\",\"value\":5000}"
            "]'"
        ),
        check=False,
    )

    # In questa modalità orchestrata il client usato e' Docker esterno.
    # Disattiva il client Kubernetes del manifest per evitare traffico di
    # background che consuma pod single-use e altera le misure.
    run(
        f"kubectl -n {quote(namespace)} scale deployment/client-kubernetes --replicas=0",
        check=False,
    )

    rollout_targets = [
        "server-kubernetes",
        OPERATOR_DEPLOYMENT,
        GATEWAY_DEPLOYMENT,
    ]

    rollout_start_ns = time.time_ns()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rollout_targets)) as executor:
        futures: dict[concurrent.futures.Future[str], str] = {
            executor.submit(
                run,
                f"kubectl -n {quote(namespace)} rollout status deployment/{name} --timeout=240s",
                check=True,
            ): name
            for name in rollout_targets
        }
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as exc:
                diagnostics = gather_rollout_diagnostics(namespace, name)
                raise RuntimeError(
                    f"Rollout fallito per deployment/{name}.\n{exc}\n\n{diagnostics}"
                ) from exc
    rollout_elapsed_ns = time.time_ns() - rollout_start_ns
    system_log(
        f"Rollout completato in {rollout_elapsed_ns} ns ({rollout_elapsed_ns / 1_000_000_000.0:.3f} s)"
    )


def get_first_operator_pod_name(namespace: str) -> str:
    output = run(
        (
            f"kubectl -n {quote(namespace)} get pods -l {quote(OPERATOR_LABEL)} "
            "-o jsonpath='{.items[0].metadata.name}'"
        ),
        check=True,
    ).strip().strip("'")
    if not output:
        raise RuntimeError("Nessun pod operator middlebox trovato")
    return output


def check_middlebox_sgx(namespace: str, operator_mode: str) -> None:
    if operator_mode != "sgx":
        append_text(MIDDLEBOX_RUNTIME_LOG, "=== SGX device check ===\n")
        append_text(MIDDLEBOX_RUNTIME_LOG, "SKIPPED (operator mode non-SGX)\n")
        return

    pod_name = get_first_operator_pod_name(namespace)
    output = run(
        f"kubectl -n {quote(namespace)} exec {quote(pod_name)} -- ls -l /dev/sgx_enclave /dev/sgx_provision",
        check=True,
    )
    append_text(MIDDLEBOX_RUNTIME_LOG, "=== SGX device check ===\n")
    append_text(MIDDLEBOX_RUNTIME_LOG, output + "\n")


def ensure_docker_network() -> None:
    result = subprocess.run(
        f"docker network inspect {quote(DOCKER_NETWORK_NAME)}",
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        system_log(f"Creo docker network {DOCKER_NETWORK_NAME}")
        run(f"docker network create {quote(DOCKER_NETWORK_NAME)}", check=True)
    else:
        system_log(f"Docker network {DOCKER_NETWORK_NAME} gia presente")


def ensure_server_base_certs_files() -> None:
    SERVER_CERTS_HOST_DIR.mkdir(parents=True, exist_ok=True)
    cert_path = SERVER_CERTS_HOST_DIR / "cert.pem"
    key_path = SERVER_CERTS_HOST_DIR / "key.pem"

    # Usa sempre una coppia di cert/key nota e compatibile con delegated credentials.
    seed_candidates = [
        (PROJECT_ROOT / "PerformanceMeasuring/certs/cert.pem", PROJECT_ROOT / "PerformanceMeasuring/certs/key.pem"),
        (PROJECT_ROOT / "DC/certs/cert.pem", PROJECT_ROOT / "DC/certs/key.pem"),
        (PROJECT_ROOT / "DC/Middlebox/certs_fuori/cert.pem", PROJECT_ROOT / "DC/Middlebox/certs_fuori/key.pem"),
    ]

    selected: tuple[Path, Path] | None = None
    for src_cert, src_key in seed_candidates:
        if src_cert.exists() and src_key.exists():
            selected = (src_cert, src_key)
            break

    if selected is None:
        if cert_path.exists() and key_path.exists():
            return
        raise RuntimeError(
            "Impossibile inizializzare cert base server: nessuna coppia cert/key seed trovata in repository."
        )

    src_cert, src_key = selected
    shutil.copyfile(src_cert, cert_path)
    shutil.copyfile(src_key, key_path)


def start_docker_server() -> None:
    system_log(f"Avvio server Docker: {SERVER_CONTAINER_NAME}")
    run(f"docker rm -f {quote(SERVER_CONTAINER_NAME)}", check=False)

    ensure_server_base_certs_files()

    # Prepara variabili d'ambiente per il container server
    env_args = (
        f"-e CERTS_DIR=/certs "
        f"-e SERVER_RUNTIME_LOG=/tmp/Server.log "
        f"-e GO_TOOL=/tmp/generate_dc_tool"
    )

    # Monta il tool generate_dc_tool se esiste
    mount_tool = ""
    if LOCAL_GENERATE_TOOL.exists():
        mount_tool = f"-v {quote(LOCAL_GENERATE_TOOL)}:/tmp/generate_dc_tool "

    run(
        (
            f"docker run -d --name {quote(SERVER_CONTAINER_NAME)} "
            f"--network {quote(DOCKER_NETWORK_NAME)} "
            f"-v {quote(SERVER_CERTS_HOST_DIR)}:/certs "
            f"{mount_tool}"
            f"-p {SERVER_DOCKER_PORT_CERTS}:{SERVER_DOCKER_PORT_CERTS} "
            f"-p {SERVER_DOCKER_PORT_APP}:{SERVER_DOCKER_PORT_APP} "
            f"{env_args} "
            f"{quote(SERVER_IMAGE)} "
            "python3 -u certs_server.py"
        ),
        check=True,
    )

    system_log("Attesa server Docker pronto (porta 5000)")
    started = time.time()
    timeout_s = 60
    while True:
        probe = subprocess.run(
            f"docker exec {quote(SERVER_CONTAINER_NAME)} bash -c \"(echo > /dev/tcp/127.0.0.1/{SERVER_DOCKER_PORT_CERTS}) >/dev/null 2>&1\"",
            shell=True,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            system_log("Server Docker pronto (TCP 5000 aperta)")
            time.sleep(1)
            return

        if time.time() - started > timeout_s:
            logs = run(f"docker logs --tail 120 {quote(SERVER_CONTAINER_NAME)}", check=False)
            stop_docker_server()
            raise RuntimeError(
                "Timeout in attesa server Docker pronto su porta 5000.\n"
                f"Container logs (tail):\n{logs}"
            )
        time.sleep(0.5)


def stop_docker_server() -> None:
    system_log(f"Fermo server Docker: {SERVER_CONTAINER_NAME}")
    run(f"docker rm -f {quote(SERVER_CONTAINER_NAME)}", check=False)


def get_gateway_nodeport(namespace: str, timeout_s: int = 30) -> int:
    """
    Recupera il NodePort del gateway.
    Se il service e' ClusterIP (nodePort vuoto), lo promuove a NodePort e attende assegnazione.
    """

    def read_nodeport() -> str:
        return run(
            (
                f"kubectl -n {quote(namespace)} get svc middlebox-kubernetes "
                "-o jsonpath='{.spec.ports[0].nodePort}'"
            ),
            check=False,
        ).strip().strip("'")

    output = read_nodeport()
    if output:
        try:
            return int(output)
        except (ValueError, TypeError):
            pass

    system_log("Service middlebox-kubernetes senza NodePort: patch type=NodePort")
    run(
        (
            f"kubectl -n {quote(namespace)} patch svc middlebox-kubernetes "
            "-p '{\"spec\":{\"type\":\"NodePort\"}}'"
        ),
        check=True,
    )

    started = time.time()
    while True:
        output = read_nodeport()
        if output:
            try:
                return int(output)
            except (ValueError, TypeError):
                pass

        if time.time() - started > timeout_s:
            svc_diag = run(
                f"kubectl -n {quote(namespace)} get svc middlebox-kubernetes -o yaml",
                check=False,
            )
            raise RuntimeError(
                "Impossibile determinare NodePort del gateway dopo patch a NodePort.\n"
                f"Valore letto: {output}\n"
                "Diagnostica service:\n"
                f"{svc_diag}"
            )

        time.sleep(0.5)


def get_node_host() -> str:
    """
    Restituisce l'host su cui sono esposti i NodePort, raggiungibile dai
    container sulla rete DOCKER_NETWORK_NAME:
    - per cluster kind: connette kind-control-plane a DOCKER_NETWORK_NAME
      (se non già connesso) e restituisce il suo IP su quella rete
    - per altri cluster: 127.0.0.1
    """
    global _NODE_HOST_CACHE
    if _NODE_HOST_CACHE:
        return _NODE_HOST_CACHE

    context = run("kubectl config current-context", check=False).strip()
    if not context.startswith("kind-"):
        _NODE_HOST_CACHE = "127.0.0.1"
        return _NODE_HOST_CACHE

    # Connetti kind-control-plane alla rete misure-kubernetes (idempotente)
    connect_result = subprocess.run(
        f"docker network connect {quote(DOCKER_NETWORK_NAME)} kind-control-plane",
        shell=True,
        capture_output=True,
        text=True,
    )
    if connect_result.returncode == 0:
        system_log(f"kind-control-plane connesso alla rete {DOCKER_NETWORK_NAME}")
    # returncode != 0 significa già connesso oppure errore: continua comunque

    # Leggi l'IP di kind-control-plane sulla rete misure-kubernetes
    node_ip = run(
        f"docker inspect kind-control-plane"
        f" --format '{{{{(index .NetworkSettings.Networks \"{DOCKER_NETWORK_NAME}\").IPAddress}}}}'",
        check=False,
    ).strip().strip("'")
    if node_ip:
        _NODE_HOST_CACHE = node_ip
        return _NODE_HOST_CACHE

    _NODE_HOST_CACHE = "127.0.0.1"
    return _NODE_HOST_CACHE


def wait_middlebox_reachable(nodeport: int, timeout_s: int = 180) -> None:
    host = get_node_host()
    system_log(f"Attesa gateway raggiungibile su {host}:{nodeport}")
    started = time.time()
    while True:
        probe = subprocess.run(
            f"bash -c \"(echo > /dev/tcp/{host}/{nodeport}) >/dev/null 2>&1\"",
            shell=True,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            system_log(f"Gateway raggiungibile (TCP {host}:{nodeport} aperta)")
            return

        if time.time() - started > timeout_s:
            raise RuntimeError(
                f"Timeout in attesa gateway raggiungibile su {host}:{nodeport}"
            )
        time.sleep(1)


def wait_server_service_ready(namespace: str, timeout_s: int = 120) -> None:
    started = time.time()
    while True:
        endpoints = run(
            f"kubectl -n {quote(namespace)} get endpoints server -o jsonpath='{{.subsets[*].addresses[*].ip}}'",
            check=False,
        ).strip().strip("'")
        if endpoints:
            return

        if time.time() - started > timeout_s:
            dep = run(
                f"kubectl -n {quote(namespace)} get deployment server-kubernetes -o wide",
                check=False,
            )
            pods = run(
                f"kubectl -n {quote(namespace)} get pods -l app=server-kubernetes -o wide",
                check=False,
            )
            logs = run(
                f"kubectl -n {quote(namespace)} logs deploy/server-kubernetes --tail=120",
                check=False,
            )
            raise RuntimeError(
                "Service server senza endpoint pronti per /certs (porta 5000).\n"
                f"deployment:\n{dep}\n"
                f"pods:\n{pods}\n"
                f"logs:\n{logs}"
            )

        time.sleep(1)


def seed_k8s_server_base_certs(namespace: str) -> None:
    ensure_server_base_certs_files()
    cert_src = SERVER_CERTS_HOST_DIR / "cert.pem"
    key_src = SERVER_CERTS_HOST_DIR / "key.pem"
    if not cert_src.exists() or not key_src.exists():
        raise RuntimeError(
            "Certificati base mancanti per server Kubernetes. Attesi file:\n"
            f"- {cert_src}\n"
            f"- {key_src}"
        )

    pod_name = run(
        (
            f"kubectl -n {quote(namespace)} get pods -l app=server-kubernetes "
            "-o jsonpath='{.items[0].metadata.name}'"
        ),
        check=True,
    ).strip().strip("'")
    if not pod_name:
        raise RuntimeError("Pod server-kubernetes non trovato per seed certificati")

    run(
        f"kubectl -n {quote(namespace)} cp {quote(cert_src)} {quote(pod_name)}:/certs/cert.pem",
        check=True,
    )
    run(
        f"kubectl -n {quote(namespace)} cp {quote(key_src)} {quote(pod_name)}:/certs/key.pem",
        check=True,
    )
    verify = run(
        f"kubectl -n {quote(namespace)} exec {quote(pod_name)} -- sh -lc 'test -s /certs/cert.pem && test -s /certs/key.pem && echo OK'",
        check=False,
    ).strip()
    if "OK" not in verify:
        raise RuntimeError("Seed certificati server-kubernetes fallito: cert/key non presenti nel pod")


def cleanup_stack(namespace: str, operator_mode: str) -> None:
    manifest = get_orchestrate_manifest(operator_mode)
    run(
        f"kubectl -n {quote(namespace)} delete -f {quote(manifest)} --ignore-not-found=true --wait=false",
        check=False,
        timeout=90,
    )
    # Best-effort cleanup of legacy resources from older manifests/runs.
    run(
        f"kubectl -n {quote(namespace)} delete deployment middlebox-kubernetes --ignore-not-found=true --wait=false",
        check=False,
        timeout=30,
    )
    run(
        f"kubectl -n {quote(namespace)} delete rs -l app=middlebox-kubernetes --ignore-not-found=true --wait=false",
        check=False,
        timeout=30,
    )
    run(
        f"kubectl -n {quote(namespace)} delete pod -l app=middlebox-kubernetes --ignore-not-found=true --wait=false",
        check=False,
        timeout=30,
    )
    stop_docker_server()


def get_operator_pods_json(namespace: str) -> list[dict]:
    output = run(
        f"kubectl -n {quote(namespace)} get pods -l {quote(OPERATOR_LABEL)} -o json",
        check=False,
    )
    if not output.strip():
        return []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return []
    return parsed.get("items", []) if isinstance(parsed, dict) else []


def is_pod_ready(pod: dict) -> bool:
    conditions = ((pod.get("status") or {}).get("conditions") or [])
    for condition in conditions:
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return True
    return False


def get_middlebox_replica_snapshot(namespace: str) -> tuple[int, int, int, int]:
    deploy_json = run(
        f"kubectl -n {quote(namespace)} get deployment {quote(OPERATOR_DEPLOYMENT)} -o json",
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


def scale_middlebox(namespace: str, new_replicas: int) -> None:
    run(
        (
            f"kubectl -n {quote(namespace)} scale deployment/{OPERATOR_DEPLOYMENT} "
            f"--replicas={max(0, new_replicas)}"
        ),
        check=True,
    )


def ensure_initial_operator_pool(namespace: str) -> None:
    system_log(f"Imposto pool iniziale middlebox repliche={INITIAL_OPERATORS}")
    scale_middlebox(namespace, INITIAL_OPERATORS)


def wait_middlebox_pool_ready(namespace: str, min_ready: int, timeout_s: int = 240) -> int:
    system_log(f"Attesa pool middlebox pronto: ready>={min_ready}")
    started = time.time()
    while True:
        ready, available, desired, pending = get_middlebox_replica_snapshot(namespace)
        if ready >= min_ready:
            system_log(
                f"Pool middlebox pronto: ready={ready} available={available} desired={desired} pending={pending}"
            )
            return ready

        now = time.time()

        if now - started > timeout_s:
            if ready > 0:
                system_log(
                    "Timeout warmup pool: continuo comunque (policy drop) con "
                    f"ready={ready}, atteso>={min_ready}"
                )
                return ready
            raise RuntimeError(
                "Timeout in attesa pool iniziale middlebox pronto senza alcun operator ready: "
                f"ready={ready} available={available} desired={desired} pending={pending}, atteso>={min_ready}"
            )

        time.sleep(1)


def wait_initial_pool_with_timing(namespace: str, replicas: int, timeout_s: int = 240) -> tuple[int, int, int]:
    start_ns = time.time_ns()
    ensure_initial_operator_pool(namespace)
    ready = wait_middlebox_pool_ready(namespace, replicas, timeout_s=timeout_s)
    elapsed_ns = time.time_ns() - start_ns
    avg_per_pod_ns = int(elapsed_ns / max(1, replicas))
    system_log(
        "Pool iniziale completo: "
        f"repliche_target={replicas} ready={ready} totale={elapsed_ns} ns "
        f"media_per_pod={avg_per_pod_ns} ns "
        f"totale_s={elapsed_ns / 1_000_000_000.0:.3f} "
        f"media_per_pod_s={avg_per_pod_ns / 1_000_000_000.0:.3f}"
    , level=LogLevel.QUIET)
    return ready, elapsed_ns, avg_per_pod_ns


def measure_single_pod_provisioning_ns(namespace: str, timeout_s: int = 240) -> int | None:
    pods_before = get_operator_pods_json(namespace)
    names_before = {((pod.get("metadata") or {}).get("name") or "") for pod in pods_before}
    names_before.discard("")

    _, _, desired, _ = get_middlebox_replica_snapshot(namespace)
    target = desired + 1
    system_log(f"Misura provisioning singolo pod: scale {desired} -> {target}")
    scale_middlebox(namespace, target)

    start_ns = time.time_ns()
    deadline = time.time() + timeout_s
    created_name: str | None = None

    while time.time() < deadline:
        pods = get_operator_pods_json(namespace)
        for pod in pods:
            name = ((pod.get("metadata") or {}).get("name") or "")
            if not name or name in names_before:
                continue
            created_name = name
            if is_pod_ready(pod):
                delta_ns = time.time_ns() - start_ns
                system_log(
                    f"Provisioning singolo pod completato: pod={created_name} delta={delta_ns} ns"
                )
                scale_middlebox(namespace, desired)
                return delta_ns
        time.sleep(0.5)

    system_log("Provisioning singolo pod non completato entro timeout")
    scale_middlebox(namespace, desired)
    return None


def autoscale_loop(namespace: str, stop_event: threading.Event, events: list[str]) -> None:
    period = AUTOSCALE_RATE_MS
    last_scale_ns = 0
    inflight_target = 0
    # SGX richiede bootstrap pesante (gramine/enclave): evitare scale command continui.
    if REFILL_COOLDOWN_OVERRIDE_S is not None:
        refill_cooldown_s = REFILL_COOLDOWN_OVERRIDE_S
    else:
        refill_cooldown_s = SGX_REFILL_COOLDOWN_S if OPERATOR_MODE == "sgx" else NOSGX_REFILL_COOLDOWN_S
    if REFILL_TARGET_READY_OVERRIDE is not None:
        refill_target_ready = max(1, REFILL_TARGET_READY_OVERRIDE)
    else:
        # Manteniamo MIN_READY_OPERATORS come soglia predittiva di refill (non aspettiamo il completo svuotamento).
        refill_target_ready = MIN_READY_OPERATORS
    while not stop_event.is_set():
        try:
            ready, available, desired, pending = get_middlebox_replica_snapshot(namespace)
            available_soon = ready + pending
            
            if available_soon >= refill_target_ready:
                # Pool tornato sopra soglia: chiudi eventuale ciclo refill precedente.
                inflight_target = 0

            if available_soon < refill_target_ready:
                now_ns = time.time_ns()

                # Se uno scale-up e' gia' partito e ci sono pod in provisioning,
                # non reinviare nuove richieste ad ogni tick.
                if inflight_target > 0 and pending > 0 and desired >= inflight_target:
                    stop_event.wait(period)
                    continue

                # Se abbiamo gia' pod in creazione, lasciare lavorare il cluster.
                if pending > 0:
                    inflight_target = max(inflight_target, desired)
                    stop_event.wait(period)
                    continue

                # Evita di emettere scale ripetuti troppo ravvicinati (runaway su SGX).
                if last_scale_ns and (now_ns - last_scale_ns) < int(refill_cooldown_s * 1_000_000_000):
                    stop_event.wait(period)
                    continue

                # Refill predittivo: calcola deficit e aggiunge almeno SCALE_UP_BY o quanto serve
                deficit = refill_target_ready - available_soon
                bump = max(SCALE_UP_BY, deficit)
                add = bump
                target = min(MAX_OPERATORS, desired + add)
                if target <= desired:
                    stop_event.wait(period)
                    continue

                scale_issue_ns = time.time_ns()
                scale_middlebox(namespace, target)
                last_scale_ns = scale_issue_ns
                inflight_target = target
                ready_after, available_after, desired_after, pending_after = get_middlebox_replica_snapshot(namespace)
                event = (
                    f"{time.strftime('%H:%M:%S')} autoscale: ready={ready} pending={pending} available_soon={available_soon} "
                    f"deficit={deficit} desired={desired} -> target={target} add={target - desired} scale_cmd_ns={scale_issue_ns} "
                    f"post_scale_pending={pending_after} post_scale_ready={ready_after} post_scale_desired={desired_after}"
                )
                events.append(event)
                system_log(
                    f"[AUTOSCALE] refill: available_soon={available_soon} < {refill_target_ready} (pending={pending}) -> {target}"
                , level=LogLevel.QUIET)
        except Exception as exc:  # noqa: BLE001
            events.append(f"{time.strftime('%H:%M:%S')} autoscale error: {exc}")
            system_log(f"[AUTOSCALE] errore: {exc}")

        stop_event.wait(period)


def send_client_request(nodeport: int, request_type: str, exp: int) -> tuple[int | None, str]:
    host = get_node_host()
    target_url = f"https://{host}:{nodeport}/function/init"
    if request_type.upper() == "POST":
        client_cmd = (
            f"cd /app && ./client -id {CLIENT_ID} -H 'Authorization : Bearer {TOKEN}' -data '{{}}' {target_url}"
        )
    else:
        client_cmd = f"cd /app && ./client -id {CLIENT_ID} -H 'Authorization : Bearer {TOKEN}' {target_url}"

    output = run(
        f"docker run --rm --network {quote(DOCKER_NETWORK_NAME)} {quote(CLIENT_IMAGE)} sh -lc {quote(client_cmd)}",
        check=False,
        timeout=120,
    )

    append_text(CLIENT_RUNTIME_LOG, f"\n===== ESPERIMENTO {exp} =====\n")
    append_text(CLIENT_RUNTIME_LOG, output + "\n")

    t1, t10 = parse_client_timestamps(output)
    if t1 is not None and t10 is not None:
        return t10 - t1, output
    return None, output


def collect_component_logs(namespace: str) -> None:
    gateway_logs = run(f"kubectl -n {quote(namespace)} logs deploy/{GATEWAY_DEPLOYMENT}", check=False)
    operator_logs = run(
        f"kubectl -n {quote(namespace)} logs -l {quote(OPERATOR_LABEL)} --prefix=true",
        check=False,
    )

    append_text(MIDDLEBOX_RUNTIME_LOG, "=== GATEWAY LOGS ===\n")
    append_text(MIDDLEBOX_RUNTIME_LOG, gateway_logs)
    append_text(MIDDLEBOX_RUNTIME_LOG, "\n=== OPERATOR LOGS ===\n")
    append_text(MIDDLEBOX_RUNTIME_LOG, operator_logs)

    # Leggi log del server Docker esterno (non K8s)
    server_logs = run(f"docker logs {quote(SERVER_CONTAINER_NAME)}", check=False)
    append_text(SERVER_RUNTIME_LOG, "=== SERVER DOCKER LOGS ===\n")
    append_text(SERVER_RUNTIME_LOG, server_logs)


def write_summary(
    *,
    totals_ns: list[int],
    ok_messages: list[int],
    missed_messages: list[int],
    request_type: str,
    operator_mode: str,
    autoscale_events: list[str],
    single_pod_provisioning_ns: int | None,
    initial_pool_bootstrap_total_ns: int | None,
    initial_pool_bootstrap_avg_per_pod_ns: int | None,
    breakdown_metrics: dict[str, list[int]],
    sent_messages: list[int],
    error_messages: list[int],
) -> None:
    with SUMMARY_FILE.open("w", encoding="utf-8") as handle:
        handle.write("Misure orchestrate client-server-middlebox HYBRID (Docker + Kubernetes)\n")
        handle.write("=====================================================================\n")
        handle.write("Architettura: Client/Server Docker (esterno), Gateway/Operator K8s (interno)\n")
        handle.write(f"Richieste schedulate: {N_REQUESTS}\n")
        handle.write(f"Richieste inviate: {len(sent_messages)}\n")
        handle.write(f"Hit (OK): {len(ok_messages)}\n")
        handle.write(f"Miss/Error: {len(missed_messages)}\n")
        handle.write(f"Error espliciti client: {len(error_messages)}\n")
        if N_REQUESTS > 0:
            hit_ratio = (len(ok_messages) / N_REQUESTS) * 100.0
            handle.write(f"Hit ratio: {hit_ratio:.1f}%\n")
        handle.write(f"Tipo richiesta: {request_type.upper()}\n")
        handle.write(f"Operator mode: {operator_mode}\n")
        handle.write(f"Rate client: 1 ogni {REQUEST_RATE_MS} ms\n")
        handle.write(f"Pool iniziale operator: {INITIAL_OPERATORS}\n")
        handle.write(f"Soglia autoscale min ready: {MIN_READY_OPERATORS}\n")
        handle.write(f"Scale up by: {SCALE_UP_BY}\n")
        handle.write(f"Max operator: {MAX_OPERATORS}\n")
        handle.write(f"Rate autoscale: ogni {AUTOSCALE_RATE_MS} ms\n")
        handle.write("Flusso: Client Docker -> Gateway K8s (NodePort) -> Operator K8s -> Server Docker\n")
        if operator_mode == "sgx":
            handle.write("Operator mode SGX: raw passthrough /dev/sgx_* attivo\n")
        else:
            handle.write("Operator mode non-SGX: stesso flusso gateway/pod single-use, senza SGX\n")
        handle.write("Autoscale loop: eseguito dall'orchestrator Python (non dal gateway)\n")
        if initial_pool_bootstrap_total_ns is not None and initial_pool_bootstrap_avg_per_pod_ns is not None:
            handle.write(
                "Bootstrap pool iniziale: "
                f"totale={initial_pool_bootstrap_total_ns} ns "
                f"media_per_pod={initial_pool_bootstrap_avg_per_pod_ns} ns "
                f"totale_s={initial_pool_bootstrap_total_ns / 1_000_000_000.0:.3f} "
                f"media_per_pod_s={initial_pool_bootstrap_avg_per_pod_ns / 1_000_000_000.0:.3f}\n"
            )
        if single_pod_provisioning_ns is not None:
            handle.write(f"Provisioning Kubernetes singolo pod operator (fuori da t10-t1): {single_pod_provisioning_ns} ns\n")
        else:
            handle.write("Provisioning Kubernetes singolo pod operator: non misurato (timeout)\n")
        handle.write("----------------------------------------\n")

        if totals_ns:
            handle.write(f"t10-t1 media: {round(statistics.fmean(totals_ns))} ns\n")
            handle.write(f"t10-t1 mediana: {round(statistics.median(totals_ns))} ns\n")
            handle.write(f"t10-t1 min: {min(totals_ns)} ns\n")
            handle.write(f"t10-t1 max: {max(totals_ns)} ns\n")
        else:
            handle.write("Nessun t10-t1 valido raccolto\n")

        handle.write("----------------------------------------\n")
        handle.write("Breakdown latenze (fuori da t10-t1)\n")
        handle.write("----------------------------------------\n")
        write_breakdown_block(
            handle,
            "1) client->gateway (t25-t1)",
            breakdown_metrics.get("client_to_gateway_latency_ns", []),
        )
        write_breakdown_block(
            handle,
            "2) scelta pod->inoltro gateway (t28-t27)",
            breakdown_metrics.get("gateway_selection_to_forward_ns", []),
        )
        write_breakdown_block(
            handle,
            "2b) inoltro gateway->ricezione operator (t29-t28)",
            breakdown_metrics.get("gateway_to_operator_delivery_ns", []),
        )
        write_breakdown_block(
            handle,
            "3) xcode validation middleware (t38-t37)",
            breakdown_metrics.get("xcode_validation_ns", []),
        )
        write_breakdown_block(
            handle,
            "4) operator->server handshake path (t4-t3)",
            breakdown_metrics.get("operator_to_server_latency_ns", []),
        )
        write_breakdown_block(
            handle,
            "5) server cert gen (t6-t5)",
            breakdown_metrics.get("server_cert_generation_ns", []),
        )

        handle.write("----------------------------------------\n")
        handle.write("Eventi autoscale\n")
        handle.write("----------------------------------------\n")
        if autoscale_events:
            for event in autoscale_events:
                handle.write(event + "\n")
        else:
            handle.write("Nessun evento autoscale\n")


def export_runtime_logs_to_txt() -> None:
    # Manteniamo solo i log runtime necessari agli esperimenti.
    for path in (CLIENT_RUNTIME_LOG, MIDDLEBOX_RUNTIME_LOG, SERVER_RUNTIME_LOG):
        if not path.exists():
            path.write_text("", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Orchestrazione misure HYBRID: client/server Docker (esterno) -> gateway/operator K8s (SGX o non-SGX)"
    )
    parser.add_argument("--requests", type=int, default=N_REQUESTS)
    parser.add_argument("--type", choices=["GET", "POST", "get", "post"], default=REQUEST_TYPE)
    parser.add_argument("--request-rate-ms", type=int, default=REQUEST_RATE_MS)
    parser.add_argument(
        "--autoscale-rate-ms",
        type=int,
        default=AUTOSCALE_RATE_MS,
        help=f"Periodo autoscale in ms (default: {AUTOSCALE_RATE_MS})",
    )
    parser.add_argument(
        "--initial-operators",
        type=int,
        default=INITIAL_OPERATORS,
        help=f"Pool iniziale operator (default: {INITIAL_OPERATORS})",
    )
    parser.add_argument(
        "--min-ready",
        type=int,
        default=MIN_READY_OPERATORS,
        help=f"Soglia min operator disponibili (default: {MIN_READY_OPERATORS})",
    )
    parser.add_argument(
        "--scale-up-by",
        type=int,
        default=SCALE_UP_BY,
        help=f"Quanti operator aggiungere sotto soglia (default: {SCALE_UP_BY})",
    )
    parser.add_argument(
        "--max-operators",
        type=int,
        default=MAX_OPERATORS,
        help=f"Numero massimo di pod operator gestiti dall'autoscale (default: {MAX_OPERATORS})",
    )
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--kind-cluster-name", default="kind")
    parser.add_argument("--no-build-images", action="store_true")
    parser.add_argument(
        "--force-rebuild-images",
        action="store_true",
        help="Compatibilita: forza rebuild (ora gia default)",
    )
    parser.add_argument(
        "--reuse-existing-images",
        action="store_true",
        help="Disattiva il rebuild forzato e riusa immagini locali gia presenti",
    )
    parser.add_argument("--skip-kind-load", action="store_true")
    parser.add_argument(
        "--measure-single-pod-provisioning",
        action="store_true",
        help="Misura esplicita del tempo create->ready di un singolo pod operator (allunga il run)",
    )
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument(
        "--operator-mode",
        choices=["sgx", "nosgx"],
        default=OPERATOR_MODE,
        help="Modalita operator: 'sgx' usa middleboxsgxshield con raw passthrough, 'nosgx' usa mb_operator senza SGX",
    )
    parser.add_argument(
        "--refill-cooldown-s",
        type=int,
        default=None,
        help=(
            "Cooldown minimo tra due scale-up del refill pool. "
            f"Default: {SGX_REFILL_COOLDOWN_S}s in SGX, {NOSGX_REFILL_COOLDOWN_S}s in no-SGX"
        ),
    )
    parser.add_argument(
        "--refill-target-ready",
        type=int,
        default=None,
        help=(
            "Numero minimo di operator disponibili che l'autoscale prova a mantenere in continuo. "
            "Default dinamico: in SGX max(min-ready, initial-operators-1), in no-SGX min-ready"
        ),
    )
    return parser.parse_args()


def main() -> None:
    global N_REQUESTS
    global REQUEST_RATE_MS
    global INITIAL_OPERATORS
    global MIN_READY_OPERATORS
    global SCALE_UP_BY
    global MAX_OPERATORS
    global AUTOSCALE_RATE_MS
    global OPERATOR_MODE
    global REFILL_COOLDOWN_OVERRIDE_S
    global REFILL_TARGET_READY_OVERRIDE
    global _NODE_HOST_CACHE

    args = parse_args()

    if args.requests <= 0:
        raise ValueError("--requests deve essere > 0")
    if args.request_rate_ms < 0 or args.autoscale_rate_ms < 0:
        raise ValueError("I rate devono essere >= 0")
    if args.initial_operators <= 0:
        raise ValueError("--initial-operators deve essere > 0")
    if args.min_ready <= 0:
        raise ValueError("--min-ready deve essere > 0")
    if args.scale_up_by <= 0:
        raise ValueError("--scale-up-by deve essere > 0")
    if args.max_operators <= 0:
        raise ValueError("--max-operators deve essere > 0")
    if args.refill_cooldown_s is not None and args.refill_cooldown_s < 0:
        raise ValueError("--refill-cooldown-s deve essere >= 0")
    if args.refill_target_ready is not None and args.refill_target_ready <= 0:
        raise ValueError("--refill-target-ready deve essere > 0")

    N_REQUESTS = args.requests
    REQUEST_RATE_MS = args.request_rate_ms
    INITIAL_OPERATORS = args.initial_operators
    MIN_READY_OPERATORS = args.min_ready
    SCALE_UP_BY = args.scale_up_by
    MAX_OPERATORS = args.max_operators
    AUTOSCALE_RATE_MS = args.autoscale_rate_ms
    OPERATOR_MODE = args.operator_mode
    REFILL_COOLDOWN_OVERRIDE_S = args.refill_cooldown_s
    REFILL_TARGET_READY_OVERRIDE = args.refill_target_ready
    _NODE_HOST_CACHE = None

    if MAX_OPERATORS < INITIAL_OPERATORS:
        raise ValueError("--max-operators deve essere >= --initial-operators")

    ensure_directories(reset=True)

    totals_ns: list[int] = []
    sent_messages: list[int] = []
    ok_messages: list[int] = []
    missed_messages: list[int] = []
    error_messages: list[int] = []
    autoscale_events: list[str] = []
    autoscale_stop_event = threading.Event()
    autoscale_thread: threading.Thread | None = None
    single_pod_provisioning_ns: int | None = None
    initial_pool_bootstrap_total_ns: int | None = None
    initial_pool_bootstrap_avg_per_pod_ns: int | None = None
    breakdown_metrics: dict[str, list[int]] = {}
    gateway_nodeport: int | None = None

    try:
        ensure_kubernetes_cluster_ready(args.namespace)

        if not args.no_build_images:
            force_rebuild = ALWAYS_REBUILD_IMAGES and not args.reuse_existing_images
            if args.force_rebuild_images:
                force_rebuild = True
            build_images(force_rebuild=force_rebuild, operator_mode=OPERATOR_MODE)

        if not args.skip_kind_load:
            maybe_load_kind_images(args.kind_cluster_name, OPERATOR_MODE)

        ensure_docker_network()
        cleanup_stack(args.namespace, OPERATOR_MODE)
        deploy_stack(args.namespace, OPERATOR_MODE)
        gateway_nodeport = get_gateway_nodeport(args.namespace)
        system_log(f"Gateway NodePort: {gateway_nodeport}", level=LogLevel.DEBUG)
        ensure_server_generate_tool()
        start_docker_server()
        seed_k8s_server_base_certs(args.namespace)
        _, initial_pool_bootstrap_total_ns, initial_pool_bootstrap_avg_per_pod_ns = wait_initial_pool_with_timing(
            args.namespace,
            MIN_READY_OPERATORS,
        )
        if args.measure_single_pod_provisioning:
            single_pod_provisioning_ns = measure_single_pod_provisioning_ns(args.namespace)
        else:
            system_log(
                "Misura provisioning singolo pod disattivata (usa --measure-single-pod-provisioning per abilitarla)"
            , level=LogLevel.DEBUG)
        check_middlebox_sgx(args.namespace, OPERATOR_MODE)
        wait_server_service_ready(args.namespace)
        if gateway_nodeport is None:
            raise RuntimeError("Gateway NodePort non determinato")
        wait_middlebox_reachable(gateway_nodeport)

        autoscale_thread = threading.Thread(
            target=autoscale_loop,
            args=(args.namespace, autoscale_stop_event, autoscale_events),
            daemon=True,
        )
        autoscale_thread.start()

        system_log(f"Attesa pool caldo a {INITIAL_OPERATORS} operator prima di traffic", level=LogLevel.QUIET)
        _ = wait_middlebox_pool_ready(args.namespace, INITIAL_OPERATORS, timeout_s=300)
        system_log("Pool caldo raggiunto, inizio traffic", level=LogLevel.QUIET)

        period_s = max(0.0, REQUEST_RATE_MS / 1000.0)
        next_dispatch = time.monotonic()
        pending_future: concurrent.futures.Future[tuple[int | None, str]] | None = None
        pending_message: int | None = None

        def finalize_pending_request(*, block: bool) -> None:
            nonlocal pending_future, pending_message
            if pending_future is None:
                return
            if not block and not pending_future.done():
                return

            total_ns, output = pending_future.result()
            message_id = pending_message

            if message_id is None:
                pending_future = None
                pending_message = None
                return

            if total_ns is not None:
                totals_ns.append(total_ns)

            is_ok = (
                '"status": "ok"' in output
                or '"status":"ok"' in output
                or "function initialized" in output
            )

            if is_ok:
                ok_messages.append(message_id)
                system_log(f"Richiesta {message_id}: OK", level=LogLevel.QUIET)
            else:
                missed_messages.append(message_id)
                error_messages.append(message_id)
                system_log(f"Richiesta {message_id}: ERRORE/MISS", level=LogLevel.QUIET)

            pending_future = None
            pending_message = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            for exp in range(1, N_REQUESTS + 1):
                # Aspetta il prossimo tick. Nel frattempo finalizza la richiesta
                # in corso non appena arriva: "OK" viene stampato al termine
                # effettivo della richiesta, "Invio" solo al tick successivo.
                while True:
                    remaining_s = next_dispatch - time.monotonic()
                    if remaining_s <= 0:
                        break
                    finalize_pending_request(block=False)
                    if pending_future is None:
                        # Richiesta già completata: aspetta il tick preciso e poi esci.
                        time.sleep(max(0.0, next_dispatch - time.monotonic()))
                        break
                    time.sleep(min(remaining_s, 0.05))

                next_dispatch += period_s

                # Sicurezza: se la richiesta è ancora in corso (durata > REQUEST_RATE_MS),
                # aspettane il completamento prima di inviare la successiva.
                finalize_pending_request(block=True)

                system_log(f"Invio richiesta {exp}/{N_REQUESTS}", level=LogLevel.QUIET)
                sent_messages.append(exp)
                pending_message = exp
                if gateway_nodeport is None:
                    raise RuntimeError("Gateway NodePort non determinato")
                pending_future = executor.submit(send_client_request, gateway_nodeport, args.type, exp)

            finalize_pending_request(block=True)

        collect_component_logs(args.namespace)
        breakdown_metrics = compute_breakdown_metrics()

    finally:
        autoscale_stop_event.set()
        if autoscale_thread is not None:
            autoscale_thread.join(timeout=2)

        write_summary(
            totals_ns=totals_ns,
            ok_messages=ok_messages,
            missed_messages=missed_messages,
            request_type=args.type,
            operator_mode=OPERATOR_MODE,
            autoscale_events=autoscale_events,
            single_pod_provisioning_ns=single_pod_provisioning_ns,
            initial_pool_bootstrap_total_ns=initial_pool_bootstrap_total_ns,
            initial_pool_bootstrap_avg_per_pod_ns=initial_pool_bootstrap_avg_per_pod_ns,
            breakdown_metrics=breakdown_metrics,
            sent_messages=sent_messages,
            error_messages=error_messages,
        )
        export_runtime_logs_to_txt()

        if not args.keep_resources:
            cleanup_stack(args.namespace, OPERATOR_MODE)

    print("MisureOrchestrateKubernetes completato")
    print(f"Totale OK: {len(ok_messages)}")
    print(f"Totale MISS/ERROR: {len(missed_messages)}")
    print(f"Runtime logs: {RUNTIME_DIR}")
    print(f"Summary: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
