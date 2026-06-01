from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import os
import re
import socket
import shlex
import shutil
import statistics
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path


############################################
# CONFIGURAZIONE
############################################

N_REQUESTS = 5
REQUEST_TYPE = "GET"
REQUEST_RATE_MS = 1000

# Keep a larger warm headroom because operators are single-use and get consumed
# at approximately request-rate pace.
INITIAL_OPERATORS = 6
MIN_READY_OPERATORS = 20
SCALE_UP_BY = 20
AUTOSCALE_RATE_MS = 1000

STACK_NAME = "tlmsp"
OVERLAY_NETWORK_NAME = "tlmsp_pool_overlay"
TOKEN = "token"
CLIENT_ID = "client"
TARGET_URL = "https://mb_gateway:9443/function/init"
SHOW_EXEC_LOGS = True
AUTOSCALE_VERBOSE_CHECKS = True
FORCE_REBUILD_IMAGES = True
MESSAGE_VERBOSE_LOGS = False
CAPTURE_EXTENDED_COMPONENT_LOGS = False
WRITE_RUNTIME_DERIVED_DELTAS = False
PROGRESS_LOG_EVERY = 1
STACK_REMOVE_TIMEOUT_S = 45
STARTUP_TIMEOUT = 60

LOG_LOCK = threading.Lock()
_RUN_ENV_CACHE: dict[str, str] | None = None
TIMESTAMP_LINE_RE = re.compile(r"\bt(?:_op)?\d+\b\s*[:=]")

# Buffer in memoria per i log runtime (scritti solo alla fine)
LOG_BUFFER_CLIENT: list[str] = []
LOG_BUFFER_GATEWAY: list[str] = []
LOG_BUFFER_OPERATOR: list[str] = []
LOG_BUFFER_SERVER: list[str] = []

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

# Root directory requested for orchestrate runs.
BASE_OUTPUT_DIR = PROJECT_ROOT / "MisureOrchestrate"


def _run_timestamp() -> str:
	# Filesystem-friendly timestamp for per-run folder.
	return datetime.now().strftime("%Y%m%d_%H%M%S")


RUN_OUTPUT_DIR = BASE_OUTPUT_DIR / _run_timestamp()
ANALYSIS_DIR = RUN_OUTPUT_DIR / "Analisi"
RUNTIME_DIR = RUN_OUTPUT_DIR / "Runtime"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
GATEWAY_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
OPERATOR_RUNTIME_LOG = RUNTIME_DIR / "Operator.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"

SUMMARY_FILE = ANALYSIS_DIR / "OrchestrateSummary.txt"
ORCHESTRATE_LOG_DIR = RUN_OUTPUT_DIR / "Orchestrate"
CLIENT_TXT_LOG = ORCHESTRATE_LOG_DIR / "ClientLog.txt"
MIDDLEBOX_TXT_LOG = ORCHESTRATE_LOG_DIR / "MiddleboxLog.txt"
SERVER_TXT_LOG = ORCHESTRATE_LOG_DIR / "ServerLog.txt"
OPERATOR_TXT_LOG = ORCHESTRATE_LOG_DIR / "OperatorLog.txt"

CLIENT_CONTAINER = "client_orchestrate"
SERVER_CONTAINER = "server_orchestrate"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"
CLIENT_BINARY = MB_DIR / "client"
SERVER_SCRIPT = SERVER_DIR / "certs_server.py"
GO_PROFESSOR = PROJECT_ROOT / "DC" / "go" / "bin" / "go"
LOCAL_GENERATE_TOOL = RUNTIME_DIR / "generate_dc_tool"

BAREMETAL_GATEWAY_HOST = "127.0.0.1"
BAREMETAL_GATEWAY_PORT = 9443

_SERVER_PROCESS: subprocess.Popen[str] | None = None


############################################
# UTILITA
############################################


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

	# Respect explicit user override first.
	if env.get("DOCKER_HOST"):
		_RUN_ENV_CACHE = env
		return _RUN_ENV_CACHE

	# Prefer current docker context when healthy.
	if _probe_docker_env(env):
		_RUN_ENV_CACHE = env
		return _RUN_ENV_CACHE

	# Fallback for Linux hosts where desktop-linux context is broken.
	fallback_socket = Path("/var/run/docker.sock")
	if fallback_socket.exists():
		fallback_env = env.copy()
		fallback_env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
		if _probe_docker_env(fallback_env):
			_RUN_ENV_CACHE = fallback_env
			return _RUN_ENV_CACHE

	_RUN_ENV_CACHE = env
	return _RUN_ENV_CACHE


def force_linux_docker_socket() -> bool:
	"""Forza DOCKER_HOST su /var/run/docker.sock se disponibile e funzionante."""
	global _RUN_ENV_CACHE
	fallback_socket = Path("/var/run/docker.sock")
	if not fallback_socket.exists():
		return False

	fallback_env = os.environ.copy()
	fallback_env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
	if not _probe_docker_env(fallback_env):
		return False

	_RUN_ENV_CACHE = fallback_env
	return True


def run(
	command: str,
	*,
	cwd: Path | None = None,
	check: bool = True,
	timeout: int | None = None,
	stream_output: bool = False,
	log_command: bool = SHOW_EXEC_LOGS,
) -> str:
	if log_command:
		system_log(f"EXEC: {command}")
	if stream_output:
		proc = subprocess.Popen(
			command,
			shell=True,
			cwd=str(cwd) if cwd else None,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			env=get_run_env(),
		)
		try:
			stdout, _ = proc.communicate(timeout=timeout)
		except subprocess.TimeoutExpired:
			proc.kill()
			stdout, _ = proc.communicate()
			raise RuntimeError(f"Comando in timeout ({timeout}s): {command}\n{stdout}")

		output = stdout or ""
		for line in output.splitlines():
			system_log(line)

		if check and proc.returncode != 0:
			raise RuntimeError(
				f"Comando fallito con codice {proc.returncode}: {command}\n{output}"
			)
		return output

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


def docker_image_exists(image_name: str) -> bool:
	result = subprocess.run(
		f"docker image inspect {quote(image_name)}",
		shell=True,
		capture_output=True,
		text=True,
		env=get_run_env(),
	)
	return result.returncode == 0


def now_stamp() -> str:
	return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def system_log(message: str) -> None:
	with LOG_LOCK:
		print(f"[{now_stamp()}] [SYSTEM] {message}")


def append_text(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("a", encoding="utf-8") as handle:
		handle.write(text)


def append_text_buffered(path: Path, text: str, buffer: list[str]) -> None:
	"""Accoda al buffer anziché scrivere su disco."""
	buffer.append(text)


def flush_log_buffers() -> None:
	"""Scrivi tutti i buffer su disco."""
	global LOG_BUFFER_CLIENT, LOG_BUFFER_GATEWAY, LOG_BUFFER_OPERATOR, LOG_BUFFER_SERVER
	
	if LOG_BUFFER_CLIENT:
		append_many(CLIENT_RUNTIME_LOG, LOG_BUFFER_CLIENT)
	if LOG_BUFFER_GATEWAY:
		append_many(GATEWAY_RUNTIME_LOG, LOG_BUFFER_GATEWAY)
	if LOG_BUFFER_OPERATOR:
		append_many(OPERATOR_RUNTIME_LOG, LOG_BUFFER_OPERATOR)
	if LOG_BUFFER_SERVER:
		append_many(SERVER_RUNTIME_LOG, LOG_BUFFER_SERVER)


def append_many(path: Path, lines: list[str]) -> None:
	"""Scrivi una lista di linee su disco in una singola operazione."""
	if not lines:
		return
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("a", encoding="utf-8") as handle:
		handle.writelines(lines)


def compact_client_output_for_log(output: str) -> str:
	kept: list[str] = []
	for line in output.splitlines():
		if TIMESTAMP_LINE_RE.search(line):
			kept.append(line)
			continue
		if '"status":' in line:
			kept.append(line)
	if not kept:
		return ""
	return "\n".join(kept) + "\n"


def percentile_ns(values: list[int], percentile: float) -> int:
	if not values:
		raise ValueError("percentile_ns richiede almeno un valore")
	if percentile <= 0:
		return min(values)
	if percentile >= 100:
		return max(values)
	ordered = sorted(values)
	index = int(round((percentile / 100.0) * (len(ordered) - 1)))
	return ordered[index]


def should_log_progress(message_id: int) -> bool:
	if MESSAGE_VERBOSE_LOGS:
		return True
	if PROGRESS_LOG_EVERY <= 1:
		return True
	if message_id <= 3 or message_id == N_REQUESTS:
		return True
	return PROGRESS_LOG_EVERY > 0 and (message_id % PROGRESS_LOG_EVERY == 0)


def ensure_directories(reset: bool) -> None:
	if reset:
		for directory in (ANALYSIS_DIR, RUNTIME_DIR, ORCHESTRATE_LOG_DIR):
			if directory.exists():
				shutil.rmtree(directory)
	for directory in (BASE_OUTPUT_DIR, RUN_OUTPUT_DIR, ANALYSIS_DIR, RUNTIME_DIR, ORCHESTRATE_LOG_DIR):
		directory.mkdir(parents=True, exist_ok=True)


def ensure_external_certificates() -> None:
	required = [
		SERVER_CERTS_HOST_DIR / "cert.pem",
		SERVER_CERTS_HOST_DIR / "key.pem",
		CLIENT_CA_HOST_PATH,
	]
	missing = [path for path in required if not path.exists()]
	if missing:
		missing_str = ", ".join(str(path) for path in missing)
		raise FileNotFoundError(f"Certificati esterni mancanti: {missing_str}")


def ensure_client_binary() -> None:
	src = MB_DIR / "client.go"
	if CLIENT_BINARY.exists() and src.exists() and CLIENT_BINARY.stat().st_mtime >= src.stat().st_mtime:
		return
	if not GO_PROFESSOR.exists():
		raise FileNotFoundError(f"Compilatore Go del professore non trovato: {GO_PROFESSOR}")
	run(
		f"{quote(GO_PROFESSOR)} build -o {quote(CLIENT_BINARY)} client.go",
		cwd=MB_DIR,
	)


def ensure_server_generate_tool() -> None:
	dc_source = PROJECT_ROOT / "DC" / "go" / "src" / "crypto" / "tls" / "generate_delegated_credential.go"
	if not GO_PROFESSOR.exists():
		raise FileNotFoundError(f"Compilatore Go del professore non trovato: {GO_PROFESSOR}")
	if not dc_source.exists():
		raise FileNotFoundError(f"Sorgente tool generate DC mancante: {dc_source}")
	run(
		f"{quote(GO_PROFESSOR)} build -o {quote(LOCAL_GENERATE_TOOL)} {quote(dc_source)}",
	)


def ensure_swarm() -> None:
	swarm_state = run("docker info --format '{{.Swarm.LocalNodeState}}'", check=False).strip().lower()
	if swarm_state == "active":
		return

	try:
		run("docker swarm init", check=True)
	except RuntimeError as exc:
		message = str(exc).lower()
		if "already part of a swarm" in message or "swarm is already active" in message:
			return

		# Docker Desktop context può rispondere a docker info ma fallire su swarm init.
		# In quel caso, riprova forzando il socket Linux locale.
		current_host = get_run_env().get("DOCKER_HOST", "docker-context-default")
		if "docker.sock" in message and current_host != "unix:///var/run/docker.sock":
			if force_linux_docker_socket():
				system_log("Swarm init fallito sul context corrente: riprovo con DOCKER_HOST=unix:///var/run/docker.sock")
				swarm_state_retry = run(
					"docker info --format '{{.Swarm.LocalNodeState}}'",
					check=False,
				).strip().lower()
				if swarm_state_retry == "active":
					return
				try:
					run("docker swarm init", check=True)
					return
				except RuntimeError as retry_exc:
					docker_host = get_run_env().get("DOCKER_HOST", "docker-context-default")
					raise RuntimeError(
						"Impossibile inizializzare Docker Swarm anche dopo fallback socket Linux. "
						f"Endpoint usato: {docker_host}\n{retry_exc}"
					) from retry_exc

		docker_host = get_run_env().get("DOCKER_HOST", "docker-context-default")
		raise RuntimeError(
			"Impossibile inizializzare Docker Swarm. "
			f"Endpoint usato: {docker_host}\n{exc}"
		) from exc


def cleanup_previous_state() -> None:
	system_log("Pulizia ambiente precedente")
	stop_server_process()
	run(f"docker stack rm {quote(STACK_NAME)}", check=False)
	wait_for_stack_removal(STACK_NAME, timeout_s=STACK_REMOVE_TIMEOUT_S)


def wait_for_stack_removal(stack_name: str, timeout_s: int) -> None:
	deadline = time.time() + timeout_s
	while time.time() < deadline:
		services = run(
			f"docker stack services {quote(stack_name)} --format '{{{{.Name}}}}'",
			check=False,
		).strip()
		if not services:
			return
		time.sleep(0.5)
	system_log(f"Avvertenza: stack {stack_name} non rimosso entro {timeout_s}s; forzando pulizia")


def emergency_cleanup() -> None:
	"""Pulizia aggressiva finale: uccide tutti i processi su porte critiche."""
	system_log("=== PULIZIA EMERGENZA ===")
	
	# Porte critiche
	critical_ports = [5000, 8000, 8443, 9443, 18080, 8088]
	for port in critical_ports:
		try:
			run(f"fuser -k {port}/tcp 2>/dev/null", check=False)
			system_log(f"Porta {port}: processi terminati")
		except Exception:
			pass
	
	# Forza rimozione di tutti i container del progetto
	try:
		stop_server_process()
		system_log("Processo server baremetal terminato")
	except Exception:
		pass
	
	# Forza rimozione stack
	try:
		run(f"docker stack rm {quote(STACK_NAME)}", check=False)
		system_log(f"Stack {STACK_NAME} rimosso (forzato)")
	except Exception:
		pass
	
	system_log("=== PULIZIA EMERGENZA COMPLETATA ===")


def verify_cleanup() -> bool:
	"""Verifica che non ci siano processi rimasti su porte critiche."""
	critical_ports = [5000, 8000, 8443, 9443, 18080, 8088]
	all_clear = True
	
	for port in critical_ports:
		proc = subprocess.run(
			f"ss -ltnp | grep :{port}",
			shell=True,
			capture_output=True,
			text=True,
			check=False
		)
		if proc.stdout.strip():
			system_log(f"ATTENZIONE: Porta {port} ancora in ascolto: {proc.stdout.strip()}")
			all_clear = False
		else:
			system_log(f"✓ Porta {port} libera")
	
	return all_clear


def build_required_images(*, force_rebuild: bool = FORCE_REBUILD_IMAGES) -> None:
	build_plan: list[tuple[str, str, Path]] = [
		(
			"mb_gateway",
			f"docker build --progress=plain -f {quote(MB_DIR / 'Dockerfile.gateway')} -t mb_gateway:latest .",
			PROJECT_ROOT,
		),
		(
			"mb_operator",
			f"docker build --progress=plain -f {quote(MB_DIR / 'Dockerfile.operator')} -t mb_operator:latest .",
			PROJECT_ROOT,
		),
	]

	for image_name, command, build_dir in build_plan:
		if docker_image_exists(image_name) and not force_rebuild:
			system_log(f"Immagine {image_name} gia presente: skip build")
			continue
		if docker_image_exists(image_name) and force_rebuild:
			system_log(f"Immagine {image_name} gia presente: rebuild forzato")

		system_log(f"Build immagine {image_name} (log live)")
		run(
			command,
			cwd=build_dir,
			stream_output=True,
		)


def ensure_required_images_exist() -> None:
	required = ["mb_gateway", "mb_operator"]
	missing = [image for image in required if not docker_image_exists(image)]
	if not missing:
		return

	missing_str = ", ".join(missing)
	raise RuntimeError(
		"Immagini Docker mancanti: "
		f"{missing_str}. "
		"Riesegui senza --no-build-images oppure costruiscile manualmente."
	)

def start_server_process() -> None:
	global _SERVER_PROCESS
	start_idx = SERVER_RUNTIME_LOG.stat().st_size if SERVER_RUNTIME_LOG.exists() else 0
	server_log = SERVER_RUNTIME_LOG.open("a", encoding="utf-8")
	env = os.environ.copy()
	env["CERTS_DIR"] = str(SERVER_CERTS_HOST_DIR)
	env["SERVER_RUNTIME_LOG"] = str(SERVER_RUNTIME_LOG)
	env["GO_TOOL"] = str(LOCAL_GENERATE_TOOL)
	process = subprocess.Popen(
		["python3", "-u", str(SERVER_SCRIPT)],
		cwd=str(SERVER_DIR),
		stdout=subprocess.DEVNULL,
		stderr=server_log,
		text=True,
		env=env,
	)
	deadline = time.time() + STARTUP_TIMEOUT
	marker = "[SERVER] Application TLS server running on :8000"
	while time.time() < deadline:
		content = SERVER_RUNTIME_LOG.read_text(encoding="utf-8", errors="replace") if SERVER_RUNTIME_LOG.exists() else ""
		if marker in content[start_idx:]:
			server_log.close()
			_SERVER_PROCESS = process
			return
		if process.poll() is not None:
			server_log.close()
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

def detect_host_ip_for_containers() -> str:
	result = subprocess.run(
		"ip route get 1.1.1.1 | awk '{print $7; exit}'",
		shell=True,
		capture_output=True,
		text=True,
		check=True,
	)
	return result.stdout.strip()

def deploy_pool(*, build_images: bool, no_ready_policy: str) -> None:
	server_host = os.environ.get("SERVER_HOST") or detect_host_ip_for_containers()

	env = {
		"STACK_NAME": STACK_NAME,
		"SERVER_HOST": server_host,
		"OVERLAY_NETWORK_NAME": OVERLAY_NETWORK_NAME,
		"WARM_REPLICAS": str(INITIAL_OPERATORS),
		"MIN_READY_OPERATORS": str(MIN_READY_OPERATORS),
		"SCALE_UP_BY": str(SCALE_UP_BY),
		"AUTOSCALE_PERIOD_SECONDS": str(max(1, AUTOSCALE_RATE_MS // 1000)),
		"POOL_STREAM_LOGS": "0",
		"POOL_CLEANUP_ON_EXIT": "0",
		"BUILD_IMAGES": "1" if build_images else "0",
		"NO_READY_OPERATOR_POLICY": no_ready_policy,
		"OPERATOR_EXIT_AFTER_REQUEST": "true",
		"OPERATOR_CONSUME_AFTER_REQUEST": "true",
		"MB_MINIMAL_LOGS": "1",
	}

	exports = " ".join(f"{k}={quote(v)}" for k, v in env.items())
	command = f"{exports} bash {quote(MB_DIR / 'run_middlebox_pool.sh')}"
	run(command, cwd=MB_DIR)


def service_name(base: str) -> str:
	return f"{STACK_NAME}_{base}"


def wait_local_tcp_ready(host: str, port: int, timeout_s: float = 30.0) -> None:
	deadline = time.time() + timeout_s
	last_error = ""
	while time.time() < deadline:
		try:
			with socket.create_connection((host, port), timeout=1.0):
				return
		except OSError as exc:
			last_error = str(exc)
			time.sleep(0.2)
	raise TimeoutError(f"Endpoint non raggiungibile {host}:{port}. Ultimo errore: {last_error}")


def wait_for_initial_middlebox_readiness() -> None:
	deadline = time.time() + STARTUP_TIMEOUT
	while time.time() < deadline:
		running, pending, _, _ = get_operator_service_snapshot()
		if running >= INITIAL_OPERATORS:
			wait_local_tcp_ready(BAREMETAL_GATEWAY_HOST, BAREMETAL_GATEWAY_PORT, timeout_s=5.0)
			return
		time.sleep(0.3)
	raise TimeoutError(
		"Pool middlebox non pronto entro timeout: "
		f"servivano almeno {INITIAL_OPERATORS} operator warm Running prima del primo invio"
	)


def start_log_stream(command: list[str], path: Path) -> subprocess.Popen[bytes]:
	path.parent.mkdir(parents=True, exist_ok=True)
	handle = path.open("ab")
	proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=get_run_env())
	proc._log_handle = handle  # type: ignore[attr-defined]
	return proc


def stop_log_stream(proc: subprocess.Popen[bytes]) -> None:
	if proc.poll() is None:
		proc.terminate()
		try:
			proc.wait(timeout=2)
		except subprocess.TimeoutExpired:
			proc.kill()
	handle = getattr(proc, "_log_handle", None)
	if handle:
		handle.close()


def append_experiment_markers(exp: int) -> None:
	global LOG_BUFFER_CLIENT, LOG_BUFFER_GATEWAY, LOG_BUFFER_OPERATOR, LOG_BUFFER_SERVER
	marker = f"\n===== ESPERIMENTO {exp} =====\n"
	append_text_buffered(CLIENT_RUNTIME_LOG, marker, LOG_BUFFER_CLIENT)
	if CAPTURE_EXTENDED_COMPONENT_LOGS:
		append_text_buffered(GATEWAY_RUNTIME_LOG, marker, LOG_BUFFER_GATEWAY)
		append_text_buffered(OPERATOR_RUNTIME_LOG, marker, LOG_BUFFER_OPERATOR)
	append_text_buffered(SERVER_RUNTIME_LOG, marker, LOG_BUFFER_SERVER)


def parse_client_timestamps(output: str) -> tuple[int | None, int | None]:
	t1: int | None = None
	t10: int | None = None
	for line in output.splitlines():
		if "t1:" in line:
			parts = line.split("=")
			if parts:
				try:
					t1 = int(parts[-1].strip().split()[0])
				except (ValueError, IndexError):
					pass
		if "t10:" in line:
			parts = line.split("=")
			if parts:
				try:
					t10 = int(parts[-1].strip().split()[0])
				except (ValueError, IndexError):
					pass
	return t1, t10


def send_client_request(request_type: str, exp: int) -> tuple[int | None, str, int]:
	url = f"https://{BAREMETAL_GATEWAY_HOST}:{BAREMETAL_GATEWAY_PORT}/function/init"
	cmd = [
		str(CLIENT_BINARY),
		"-id",
		CLIENT_ID,
		"-ca",
		str(CLIENT_CA_HOST_PATH),
		"-servername",
		"server",
		"-H",
		f"Authorization: Bearer {TOKEN}",
	]
	if request_type.upper() == "POST":
		cmd.extend(["-data", "{}"])
	output_result = subprocess.run(
		[*cmd, url],
		cwd=str(MB_DIR),
		capture_output=True,
		text=True,
		timeout=90,
	)
	output = (output_result.stdout or "") + (output_result.stderr or "")
	completed_ns = time.time_ns()

	compact = compact_client_output_for_log(output)
	if compact:
		append_text(CLIENT_RUNTIME_LOG, compact)

	t1, t10 = parse_client_timestamps(output)
	total = None
	if t1 is not None and t10 is not None:
		total = t10 - t1
		if WRITE_RUNTIME_DERIVED_DELTAS:
			append_text(CLIENT_RUNTIME_LOG, f"t10 - t1 = {total} ns\n")
	return total, output, completed_ns


def export_runtime_logs_to_txt() -> None:
	shutil.copyfile(CLIENT_RUNTIME_LOG, CLIENT_TXT_LOG)
	shutil.copyfile(SERVER_RUNTIME_LOG, SERVER_TXT_LOG)
	if GATEWAY_RUNTIME_LOG.exists():
		shutil.copyfile(GATEWAY_RUNTIME_LOG, MIDDLEBOX_TXT_LOG)
	else:
		MIDDLEBOX_TXT_LOG.write_text("", encoding="utf-8")
	if OPERATOR_RUNTIME_LOG.exists():
		shutil.copyfile(OPERATOR_RUNTIME_LOG, OPERATOR_TXT_LOG)
	else:
		OPERATOR_TXT_LOG.write_text("", encoding="utf-8")


def get_operator_service_snapshot() -> tuple[int, int, int, set[str]]:
	svc = service_name("mb_operator_warm")
	output = run(
		f"docker service ps {quote(svc)} --no-trunc --filter desired-state=running --format '{{{{.ID}}}}|{{{{.CurrentState}}}}'",
		check=False,
	)
	total = 0
	running = 0
	running_task_ids: set[str] = set()
	for line in output.splitlines():
		parts = line.split("|", 1)
		if len(parts) != 2:
			continue
		task_id = parts[0].strip()
		current_state = parts[1].strip()
		total += 1
		if "Running" in current_state:
			running += 1
			if task_id:
				running_task_ids.add(task_id)
	pending = max(0, total - running)
	desired = total
	return running, pending, desired, running_task_ids


def get_desired_operator_replicas() -> int:
	svc = service_name("mb_operator_warm")
	output = run(
		f"docker service inspect {quote(svc)} --format '{{{{.Spec.Mode.Replicated.Replicas}}}}'",
		check=False,
	).strip()
	if not output:
		return 0
	try:
		return int(output)
	except ValueError:
		return 0


def scale_operators(new_replicas: int) -> int:
	svc = service_name("mb_operator_warm")
	issued_ns = time.time_ns()
	run(f"docker service scale --detach=true {quote(svc)}={new_replicas}", check=True)
	return issued_ns


def autoscale_loop(
	stop_event: threading.Event,
	events: list[str],
	provisioning_deltas_ns: list[int],
) -> None:
	period = max(0.05, AUTOSCALE_RATE_MS / 1000.0)
	pending_scale_issue_ns: deque[int] = deque()
	_, _, _, seen_running_tasks = get_operator_service_snapshot()
	awaiting_scale_effect = False
	awaiting_since_ns: int | None = None
	while not stop_event.is_set():
		try:
			running, pending, _, current_running_tasks = get_operator_service_snapshot()
			desired = get_desired_operator_replicas()
			available_soon = running + pending
			new_running_tasks = current_running_tasks - seen_running_tasks
			if new_running_tasks:
				awaiting_scale_effect = False
				awaiting_since_ns = None
			for task_id in sorted(new_running_tasks):
				observed_running_ns = time.time_ns()
				if pending_scale_issue_ns:
					scale_issue_ns = pending_scale_issue_ns.popleft()
					delta_ns = observed_running_ns - scale_issue_ns
					provisioning_deltas_ns.append(delta_ns)
					events.append(
						f"{time.strftime('%H:%M:%S')} swarm-task-running: task={task_id[:12]} delta_ns={delta_ns}"
					)
					if AUTOSCALE_VERBOSE_CHECKS:
						system_log(
							f"[SWARM] task {task_id[:12]} Running, create->running delta={delta_ns / 1_000_000.0:.1f} ms"
						)
			seen_running_tasks = current_running_tasks

			if AUTOSCALE_VERBOSE_CHECKS:
				system_log(
					f"[AUTOSCALE] check: running={running}, pending={pending}, desired={desired}, available_soon={available_soon}, threshold={MIN_READY_OPERATORS}"
				)

			if available_soon < MIN_READY_OPERATORS:
				now_ns = time.time_ns()
		
				deficit = MIN_READY_OPERATORS - available_soon
				bump = max(SCALE_UP_BY, deficit)
				target = desired + bump
				replicas_added = max(0, target - desired)
				scale_issue_ns = now_ns
				system_log(
					f"[AUTOSCALE] refill predittivo: available_soon={available_soon} < {MIN_READY_OPERATORS} -> desired {desired} -> {target}"
				)
				scale_issue_ns = scale_operators(target)
				for _ in range(replicas_added):
					pending_scale_issue_ns.append(scale_issue_ns)
				awaiting_scale_effect = True
				awaiting_since_ns = scale_issue_ns
				events.append(
					f"{time.strftime('%H:%M:%S')} autoscale: running={running} pending={pending} desired={desired} -> target={target} add={replicas_added} scale_cmd_ns={scale_issue_ns}"
				)
			elif AUTOSCALE_VERBOSE_CHECKS:
				system_log("[AUTOSCALE] nessuna azione: operator disponibili sopra/su soglia")
		except Exception as exc:  # noqa: BLE001
			system_log(f"[AUTOSCALE] errore: {exc}")
			events.append(f"{time.strftime('%H:%M:%S')} autoscale error: {exc}")

		stop_event.wait(period)


def write_summary(
	*,
	totals_ns: list[int],
	autoscale_events: list[str],
	provisioning_deltas_ns: list[int],
	request_type: str,
	sent_messages: list[int],
	missed_messages: list[int],
	ok_messages: list[int],
	error_messages: list[int],
) -> None:
	with SUMMARY_FILE.open("w", encoding="utf-8") as handle:
		handle.write("Misure orchestrate client-gateway-operator\n")
		handle.write("========================================\n")
		handle.write(f"Richieste schedulate: {N_REQUESTS}\n")
		handle.write(f"Hit (OK): {len(ok_messages)}\n")
		handle.write(f"Miss (saltate o fallite): {len(missed_messages)}\n")
		handle.write(f"Richieste inviate: {len(sent_messages)}\n")
		handle.write(f"Errori runtime (diagnostica): {len(error_messages)}\n")
		if N_REQUESTS > 0:
			hit_ratio = (len(ok_messages) / N_REQUESTS) * 100.0
			handle.write(f"Hit ratio: {hit_ratio:.1f}%\n")
		if missed_messages:
			missed_text = ", ".join(str(message) for message in missed_messages)
			handle.write(f"Messaggi miss: {missed_text}\n")
		handle.write(f"Tipo richiesta: {request_type.upper()}\n")
		handle.write(f"Rate client: 1 ogni {REQUEST_RATE_MS} ms\n")
		handle.write(f"Pool iniziale operator: {INITIAL_OPERATORS}\n")
		handle.write(f"Soglia autoscale: {MIN_READY_OPERATORS}\n")
		handle.write(f"Scale up by: {SCALE_UP_BY}\n")
		handle.write(f"Rate autoscale: ogni {AUTOSCALE_RATE_MS} ms\n")
		handle.write("----------------------------------------\n")

		if totals_ns:
			handle.write(f"t10-t1 media: {round(statistics.fmean(totals_ns))} ns\n")
			handle.write(f"t10-t1 mediana: {round(statistics.median(totals_ns))} ns\n")
			handle.write(f"t10-t1 min: {min(totals_ns)} ns\n")
			handle.write(f"t10-t1 max: {max(totals_ns)} ns\n")
		else:
			handle.write("Nessun t10-t1 valido raccolto\n")

		handle.write("----------------------------------------\n")
		handle.write("Provisioning operator (scale->running osservato)\n")
		handle.write("----------------------------------------\n")
		if provisioning_deltas_ns:
			p95_ns = percentile_ns(provisioning_deltas_ns, 95)
			p99_ns = percentile_ns(provisioning_deltas_ns, 99)
			handle.write(
				f"Campioni: {len(provisioning_deltas_ns)} | media: {statistics.fmean(provisioning_deltas_ns) / 1_000_000.0:.3f} ms | mediana: {statistics.median(provisioning_deltas_ns) / 1_000_000.0:.3f} ms\n"
			)
			handle.write(
				f"min: {min(provisioning_deltas_ns) / 1_000_000.0:.3f} ms | max: {max(provisioning_deltas_ns) / 1_000_000.0:.3f} ms\n"
			)
			handle.write(
				f"p95: {p95_ns / 1_000_000.0:.3f} ms | p99: {p99_ns / 1_000_000.0:.3f} ms\n"
			)
			handle.write(
				"Definizione: tempo osservato dal comando 'docker service scale' all'osservazione del task in stato Running (misura poll-based).\n"
			)
		else:
			handle.write("Nessun campione rilevato (nessun scale-up o task non osservati).\n")

		handle.write("----------------------------------------\n")
		handle.write("Eventi autoscale\n")
		handle.write("----------------------------------------\n")
		if autoscale_events:
			for event in autoscale_events:
				handle.write(event + "\n")
		else:
			handle.write("Nessun evento autoscale\n")


def run_experiment(
	*, request_type: str, build_images: bool, force_rebuild_images: bool
) -> tuple[int, int, int]:
	global LOG_BUFFER_CLIENT, LOG_BUFFER_GATEWAY, LOG_BUFFER_OPERATOR, LOG_BUFFER_SERVER
	
	# Reset buffer all'inizio della run
	LOG_BUFFER_CLIENT = []
	LOG_BUFFER_GATEWAY = []
	LOG_BUFFER_OPERATOR = []
	LOG_BUFFER_SERVER = []
	
	system_log("================ START MisureOrchestrate ================")
	system_log(
		f"N_MESSAGES={N_REQUESTS} CLIENT_ID={CLIENT_ID} URL=https://{BAREMETAL_GATEWAY_HOST}:{BAREMETAL_GATEWAY_PORT}/function/init"
	)
	system_log(f"REQUEST_RATE_MS={REQUEST_RATE_MS} ms (invio client ogni REQUEST_RATE_MS)")
	if REQUEST_RATE_MS == 0:
		system_log("REQUEST_RATE_MS=0: invio immediato senza attesa tra dispatch")
	system_log(
		f"Pool operator: initial={INITIAL_OPERATORS} threshold={MIN_READY_OPERATORS} scale_up_by={SCALE_UP_BY}"
	)
	system_log(f"Log client: {CLIENT_TXT_LOG}")
	system_log(f"Log middlebox: {MIDDLEBOX_TXT_LOG}")
	system_log(f"Log server: {SERVER_TXT_LOG}")
	system_log("Policy no-ready-operator: drop (nessuna attesa)")
	system_log("Policy operator: single-use (no reuse, terminate after each message)")
	system_log(
		f"Autoscale loop: check ogni {AUTOSCALE_RATE_MS}ms, scala di {SCALE_UP_BY} solo se running < {MIN_READY_OPERATORS}"
	)

	ensure_directories(reset=True)
	ensure_external_certificates()
	ensure_client_binary()
	ensure_server_generate_tool()
	ensure_swarm()
	# cleanup_previous_state()

	if build_images:
		build_required_images(force_rebuild=force_rebuild_images)
	else:
		ensure_required_images_exist()

	system_log("Avvio middlebox pool Docker Swarm")
	deploy_pool(
		build_images=False,
		no_ready_policy="drop",
	)
	system_log(f"Pool operator iniziale richiesto: {INITIAL_OPERATORS}")
	system_log("Avvio stream log gateway/operator")
	system_log("Avvio server baremetal")
	start_server_process()
	system_log("Server baremetal avviato")
	system_log("Attesa readiness iniziale gateway/operator")
	wait_for_initial_middlebox_readiness()
	system_log("Gateway e pool warm pronti")

	log_procs: list[subprocess.Popen[bytes]] = []
	stop_event = threading.Event()
	autoscale_events: list[str] = []
	provisioning_deltas_ns: list[int] = []
	totals_ns: list[int] = []
	sent_messages: list[int] = []
	missed_messages: list[int] = []
	ok_messages: list[int] = []
	error_messages: list[int] = []
	pending_future: Future[tuple[int | None, str, int]] | None = None
	pending_message: int | None = None
	request_dispatch_ns: dict[int, int] = {}
	executor = ThreadPoolExecutor(max_workers=1)

	def finalize_pending_request(*, block: bool) -> None:
		nonlocal pending_future
		nonlocal pending_message
		if pending_future is None:
			return
		if not block and not pending_future.done():
			return

		message_id = pending_message
		try:
			total, output, completed_ns = pending_future.result()
		except Exception as exc:  # noqa: BLE001
			total = None
			output = f"ERROR: richiesta terminata con eccezione: {exc}"
			completed_ns = time.time_ns()

		if total is not None:
			totals_ns.append(total)
		if message_id is not None:
			dispatch_ns = request_dispatch_ns.pop(message_id, None)
			if dispatch_ns is not None:
				append_text_buffered(
					CLIENT_RUNTIME_LOG,
					f"t32: [ORCH] - request_completed = {completed_ns} ns\n",
					LOG_BUFFER_CLIENT
				)
				if WRITE_RUNTIME_DERIVED_DELTAS:
					append_text_buffered(
						CLIENT_RUNTIME_LOG,
						f"t32 - t31 = {completed_ns - dispatch_ns} ns\n",
						LOG_BUFFER_CLIENT
					)

		ok_markers = (
			'"status": "ok"',
			'"status":"ok"',
			"function initialized",
		)
		is_ok = any(marker in output for marker in ok_markers)

		if is_ok:
			if message_id is not None and should_log_progress(message_id):
				system_log(f"Richiesta {message_id}: OK")
			if message_id is not None:
				ok_messages.append(message_id)
		else:
			system_log(f"Richiesta {message_id}: ERRORE")
			if message_id is not None:
				missed_messages.append(message_id)
				error_messages.append(message_id)
			append_text_buffered(
				CLIENT_RUNTIME_LOG,
				f"WARN: risposta client non standard per richiesta {message_id}: {output[:240]}\n",
				LOG_BUFFER_CLIENT
			)

		pending_future = None
		pending_message = None

	final_ok = 0
	final_miss = 0
	final_errors = 0

	try:
		if CAPTURE_EXTENDED_COMPONENT_LOGS:
			log_procs.append(
				start_log_stream(
					["docker", "service", "logs", "-f", "--raw", service_name("mb_gateway")],
					GATEWAY_RUNTIME_LOG,
				)
			)
			log_procs.append(
				start_log_stream(
					["docker", "service", "logs", "-f", "--raw", service_name("mb_operator_warm")],
					OPERATOR_RUNTIME_LOG,
				)
			)

		autoscale_thread = threading.Thread(
			target=autoscale_loop,
			args=(stop_event, autoscale_events, provisioning_deltas_ns),
			daemon=True,
		)
		autoscale_thread.start()

		period = max(0.0, REQUEST_RATE_MS / 1000.0)
		next_dispatch_time = time.monotonic()

		for exp in range(1, N_REQUESTS + 1):
			if period > 0:
				now = time.monotonic()
				if now < next_dispatch_time:
					time.sleep(next_dispatch_time - now)
				next_dispatch_time += period

			# Close the previous request before opening the next experiment marker,
			# so completion timestamps (t32) stay in the correct message section.
			finalize_pending_request(block=False)

			append_experiment_markers(exp)
			append_text_buffered(CLIENT_RUNTIME_LOG, f"--- OPERAZIONE 1 (esperimento {exp}) ---\n", LOG_BUFFER_CLIENT)
			tick_reached_ns = time.time_ns()
			append_text_buffered(
				CLIENT_RUNTIME_LOG,
				f"t30: [ORCH] - pacing_tick_reached = {tick_reached_ns} ns\n",
				LOG_BUFFER_CLIENT
			)
			if MESSAGE_VERBOSE_LOGS:
				system_log(
					f"Pre-invio messaggio {exp}: pacing rate={REQUEST_RATE_MS}ms"
				)
				system_log(f"======= MESSAGGIO {exp} ========")

			if pending_future is not None:
				missed_messages.append(exp)
				miss_ns = time.time_ns()
				append_text_buffered(
					CLIENT_RUNTIME_LOG,
					f"t33: [ORCH] - miss_decision = {miss_ns} ns\n",
					LOG_BUFFER_CLIENT
				)
				if WRITE_RUNTIME_DERIVED_DELTAS:
					append_text_buffered(
						CLIENT_RUNTIME_LOG,
						f"t33 - t30 = {miss_ns - tick_reached_ns} ns\n",
						LOG_BUFFER_CLIENT
					)
				system_log(
					f"Richiesta {exp}/{N_REQUESTS}: MISS (messaggio precedente ancora in corso)"
				)
				append_text_buffered(
					CLIENT_RUNTIME_LOG,
					"MISS: richiesta non inviata per overlap con la precedente\n",
					LOG_BUFFER_CLIENT
				)
				continue

			if should_log_progress(exp):
				system_log(f"Invio richiesta {exp}/{N_REQUESTS} da client")
			sent_messages.append(exp)
			dispatch_ns = time.time_ns()
			request_dispatch_ns[exp] = dispatch_ns
			append_text_buffered(
				CLIENT_RUNTIME_LOG,
				f"t31: [ORCH] - request_dispatched = {dispatch_ns} ns\n",
				LOG_BUFFER_CLIENT
			)
			if WRITE_RUNTIME_DERIVED_DELTAS:
				append_text_buffered(
					CLIENT_RUNTIME_LOG,
					f"t31 - t30 = {dispatch_ns - tick_reached_ns} ns\n",
					LOG_BUFFER_CLIENT
				)
			pending_message = exp
			pending_future = executor.submit(send_client_request, request_type, exp)

	finally:
		finalize_pending_request(block=True)
		executor.shutdown(wait=False)
		stop_event.set()
		for proc in log_procs:
			stop_log_stream(proc)
		
		# Scrivi tutti i buffer su disco
		flush_log_buffers()

		write_summary(
			totals_ns=totals_ns,
			autoscale_events=autoscale_events,
			provisioning_deltas_ns=provisioning_deltas_ns,
			request_type=request_type,
			sent_messages=sent_messages,
			missed_messages=missed_messages,
			ok_messages=ok_messages,
			error_messages=error_messages,
		)
		system_log(
			f"Riepilogo finale: OK={len(ok_messages)} MISS={len(missed_messages)} ERRORI={len(error_messages)}"
		)
		final_ok = len(ok_messages)
		final_miss = len(missed_messages)
		final_errors = len(error_messages)

		export_runtime_logs_to_txt()
		system_log("Log TXT esportati; eseguire GraficiOrchestrate.py per generare i grafici")

		cleanup_previous_state()
		system_log("Misure orchestrate completate")

	return final_ok, final_miss, final_errors


############################################
# CLI
############################################


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description=(
			"Orchestrazione client->gateway->operator con pool iniziale e autoscale loop parallelo"
		)
	)
	parser.add_argument(
		"--requests",
		type=int,
		default=N_REQUESTS,
		help=f"Numero richieste client (default: {N_REQUESTS})",
	)
	parser.add_argument(
		"--type",
		choices=["GET", "POST", "get", "post"],
		default=REQUEST_TYPE,
		help=f"Tipo richiesta client (default: {REQUEST_TYPE})",
	)
	parser.add_argument(
		"--request-rate-ms",
		type=int,
		default=REQUEST_RATE_MS,
		help=f"Rate client in ms (default: {REQUEST_RATE_MS})",
	)
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
		help=f"Pool iniziale operator pronti (default: {INITIAL_OPERATORS})",
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
		help=f"Quanti operator aggiungere quando sotto soglia (default: {SCALE_UP_BY})",
	)
	parser.add_argument(
		"--no-build-images",
		action="store_true",
		help="Salta la build di client/server (default: build attiva)",
	)
	parser.add_argument(
		"--force-rebuild-images",
		action="store_true",
		default=FORCE_REBUILD_IMAGES,
		help="Forza rebuild immagini anche se gia presenti (utile dopo modifiche codice)",
	)
	parser.add_argument(
		"--no-extended-component-logs",
		action="store_true",
		help=(
			"Disattiva i log gateway/operator. Se attivo, metriche come t25-t20, "
			"t27-t26, t29-t28 risulteranno senza dati."
		),
	)
	parser.add_argument(
		"--write-derived-deltas",
		action="store_true",
		help=(
			"Scrive anche differenze derivate (es. t31-t30) nei log runtime. "
			"Default: disattivo per ridurre I/O; le differenze vengono calcolate in post-processing."
		),
	)
	parser.add_argument(
		"--progress-log-every",
		type=int,
		default=PROGRESS_LOG_EVERY,
		help=(
			"Log di progresso ogni N messaggi (default: 10). "
			"Usa 0 per mostrare solo i primi/ultimi messaggi e gli errori."
		),
	)
	return parser.parse_args()


def main() -> None:
	global N_REQUESTS
	global REQUEST_RATE_MS
	global AUTOSCALE_RATE_MS
	global INITIAL_OPERATORS
	global MIN_READY_OPERATORS
	global SCALE_UP_BY
	global CAPTURE_EXTENDED_COMPONENT_LOGS
	global WRITE_RUNTIME_DERIVED_DELTAS
	global PROGRESS_LOG_EVERY

	args = parse_args()

	if args.requests <= 0:
		raise ValueError("--requests deve essere > 0")
	if args.request_rate_ms < 0 or args.autoscale_rate_ms < 0:
		raise ValueError("I rate devono essere >= 0")

	N_REQUESTS = args.requests
	REQUEST_RATE_MS = args.request_rate_ms
	AUTOSCALE_RATE_MS = args.autoscale_rate_ms
	INITIAL_OPERATORS = args.initial_operators
	MIN_READY_OPERATORS = args.min_ready
	SCALE_UP_BY = args.scale_up_by
	CAPTURE_EXTENDED_COMPONENT_LOGS = not args.no_extended_component_logs
	WRITE_RUNTIME_DERIVED_DELTAS = args.write_derived_deltas
	PROGRESS_LOG_EVERY = args.progress_log_every

	ok_count = 0
	miss_count = 0
	error_count = 0
	
	try:
		ok_count, miss_count, error_count = run_experiment(
			request_type=args.type.upper(),
			build_images=not args.no_build_images,
			force_rebuild_images=args.force_rebuild_images,
		)
	except Exception as exc:
		system_log(f"ERRORE durante run_experiment: {exc}")
		import traceback
		traceback.print_exc()
	finally:
		# Pulizia aggressiva finale SEMPRE eseguita
		try:
			emergency_cleanup()
			verify_cleanup()
		except Exception as exc:
			system_log(f"Errore durante emergency_cleanup: {exc}")

	print("MisureOrchestrate completato")
	print(f"Totale OK: {ok_count}")
	print(f"Totale MISS: {miss_count}")
	print(f"Totale ERRORI: {error_count}")
	print(f"Runtime logs: {RUNTIME_DIR}")
	print(f"Summary: {SUMMARY_FILE}")


if __name__ == "__main__":
	main()
