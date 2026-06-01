from __future__ import annotations

import importlib
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path


# Parametri richiesti
INDIRIZZO_MIDDLEBOX = "10.79.1.175"
INDIRIZZO_SERVER = "10.79.1.208"

# Topologia client/middlebox/server
CLIENT_PUBLIC_IP = "10.79.1.165"
CLIENT_MIDDLEBOX_FACING_IP = "172.16.1.2"
MIDDLEBOX_PUBLIC_IP = INDIRIZZO_MIDDLEBOX
MIDDLEBOX_CLIENT_FACING_IP = "172.16.1.1"
MIDDLEBOX_SERVER_FACING_IP = "172.16.0.1"
SERVER_PUBLIC_IP = INDIRIZZO_SERVER
SERVER_MIDDLEBOX_FACING_IP = "172.16.0.2"

SSH_USER = "bonsai"
SSH_KEY_PATH = Path.home() / ".ssh" / "id_ed25519_masterthesis"
N = 100
TYPE = "GET"
OPS = [1, 2, 3, 4]
TOKEN = "token"
NETWORK = "tlmsp-net"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT: Path | None = None
for candidate in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
    if (candidate / "PerformanceMeasuring").exists() and (candidate / "DC" / "Middlebox").exists():
        PROJECT_ROOT = candidate
        break
if PROJECT_ROOT is None:
    PROJECT_ROOT = SCRIPT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SERVER_DIR = PROJECT_ROOT / "PerformanceMeasuring"
MB_DIR = PROJECT_ROOT / "DC" / "Middlebox"
GO_PROFESSOR = PROJECT_ROOT / "DC" / "go" / "bin" / "go"

REMOTE_PROJECT_ROOT_MIDDLEBOX = "MasterThesis"
REMOTE_PROJECT_ROOT_SERVER = "MasterThesis"

REMOTE_SERVER_DIR = f"{REMOTE_PROJECT_ROOT_SERVER}/PerformanceMeasuring"
REMOTE_MB_DIR = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/Middlebox"
REMOTE_GO_PROFESSOR = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/go/bin/go"
REMOTE_SERVER_CERTS_DIR = f"{REMOTE_PROJECT_ROOT_SERVER}/certs_external/server"

REMOTE_BASE_DIR_MIDDLEBOX = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/TreDispositivi/Misure"
REMOTE_BASE_DIR_SERVER = f"{REMOTE_PROJECT_ROOT_SERVER}/TreDispositivi/Misure"
REMOTE_RUNTIME_DIR_MIDDLEBOX = f"{REMOTE_BASE_DIR_MIDDLEBOX}/Runtime"
REMOTE_RUNTIME_DIR_SERVER = f"{REMOTE_BASE_DIR_SERVER}/Runtime"
REMOTE_ANALYSIS_DIR_MIDDLEBOX = f"{REMOTE_BASE_DIR_MIDDLEBOX}/Analisi"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"

CLIENT_BINARY = MB_DIR / "client"
MIDDLEBOX_BINARY = MB_DIR / "middlebox"
REMOTE_MIDDLEBOX_BINARY = f"{REMOTE_MB_DIR}/middlebox"
LOCAL_CERTS_SERVER_SCRIPT = SERVER_DIR / "certs_server.py"
REMOTE_CERTS_SERVER_SCRIPT = f"{REMOTE_SERVER_DIR}/certs_server.py"
REMOTE_GENERATE_TOOL = f"{REMOTE_RUNTIME_DIR_SERVER}/generate_dc_tool"

BASE_DIR = PROJECT_ROOT / "TreDispositivi" / "Misure"
ANALYSIS_DIR = BASE_DIR / "Analisi"
RUNTIME_DIR = BASE_DIR / "Runtime"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"
LOCAL_GENERATE_TOOL = RUNTIME_DIR / "generate_dc_tool"

CONTAINER_TIMES_LOG = BASE_DIR / "Log_container.txt"

REMOTE_SERVER_RUNTIME_LOG = f"{REMOTE_RUNTIME_DIR_SERVER}/Server.log"
REMOTE_MIDDLEBOX_RUNTIME_LOG = f"{REMOTE_RUNTIME_DIR_MIDDLEBOX}/Middlebox.log"
REMOTE_CONTAINER_TIMES_LOG = f"{REMOTE_BASE_DIR_MIDDLEBOX}/Log_container.txt"
REMOTE_SERVER_PIDFILE = f"{REMOTE_BASE_DIR_SERVER}/server.pid"

MIDDLEBOX_CONTAINER = "middlebox"
MIDDLEBOX_IMAGE = "middlebox"
SHARED_VOLUME = "/shared"

STARTUP_TIMEOUT = 300
SETTLE_TIMEOUT = 10
SETTLE_INTERVAL = 0.2
SETTLE_STABLE_FOR = 0.5

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

_SSH_MODE_CACHE: dict[str, str] = {}


def refresh_remote_paths() -> None:
    global REMOTE_SERVER_DIR, REMOTE_MB_DIR, REMOTE_GO_PROFESSOR, REMOTE_SERVER_CERTS_DIR
    global REMOTE_BASE_DIR_MIDDLEBOX, REMOTE_BASE_DIR_SERVER
    global REMOTE_RUNTIME_DIR_MIDDLEBOX, REMOTE_RUNTIME_DIR_SERVER
    global REMOTE_ANALYSIS_DIR_MIDDLEBOX
    global REMOTE_SERVER_RUNTIME_LOG, REMOTE_MIDDLEBOX_RUNTIME_LOG
    global REMOTE_CONTAINER_TIMES_LOG, REMOTE_SERVER_PIDFILE

    REMOTE_SERVER_DIR = f"{REMOTE_PROJECT_ROOT_SERVER}/PerformanceMeasuring"
    REMOTE_MB_DIR = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/Middlebox"
    REMOTE_GO_PROFESSOR = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/go/bin/go"
    REMOTE_SERVER_CERTS_DIR = f"{REMOTE_PROJECT_ROOT_SERVER}/certs_external/server"

    REMOTE_BASE_DIR_MIDDLEBOX = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/TreDispositivi/Misure"
    REMOTE_BASE_DIR_SERVER = f"{REMOTE_PROJECT_ROOT_SERVER}/TreDispositivi/Misure"
    REMOTE_RUNTIME_DIR_MIDDLEBOX = f"{REMOTE_BASE_DIR_MIDDLEBOX}/Runtime"
    REMOTE_RUNTIME_DIR_SERVER = f"{REMOTE_BASE_DIR_SERVER}/Runtime"
    REMOTE_ANALYSIS_DIR_MIDDLEBOX = f"{REMOTE_BASE_DIR_MIDDLEBOX}/Analisi"

    REMOTE_SERVER_RUNTIME_LOG = f"{REMOTE_RUNTIME_DIR_SERVER}/Server.log"
    REMOTE_MIDDLEBOX_RUNTIME_LOG = f"{REMOTE_RUNTIME_DIR_MIDDLEBOX}/Middlebox.log"
    REMOTE_CONTAINER_TIMES_LOG = f"{REMOTE_BASE_DIR_MIDDLEBOX}/Log_container.txt"
    REMOTE_SERVER_PIDFILE = f"{REMOTE_BASE_DIR_SERVER}/server.pid"


def discover_remote_project_root(host: str, *, require_middlebox_tree: bool) -> str:
    expected_root = "MasterThesis"
    if require_middlebox_tree:
        check = (
            f"[ -d {quote(expected_root + '/PerformanceMeasuring')} ] && "
            f"[ -d {quote(expected_root + '/DC/Middlebox')} ]"
        )
    else:
        check = f"[ -d {quote(expected_root + '/PerformanceMeasuring')} ]"

    output = run_ssh(host, f"if {check}; then echo {quote(expected_root)}; else exit 1; fi", check=False).strip()
    if output:
        return expected_root

    raise RuntimeError(
        f"Path remota attesa non trovata su {host}: {expected_root}. "
        "La repository deve essere clonata in ~/MasterThesis su tutti i dispositivi."
    )


def resolve_remote_paths() -> None:
    global REMOTE_PROJECT_ROOT_MIDDLEBOX, REMOTE_PROJECT_ROOT_SERVER

    middlebox_root = discover_remote_project_root(INDIRIZZO_MIDDLEBOX, require_middlebox_tree=True)
    server_root = discover_remote_project_root(INDIRIZZO_SERVER, require_middlebox_tree=False)

    REMOTE_PROJECT_ROOT_MIDDLEBOX = middlebox_root
    REMOTE_PROJECT_ROOT_SERVER = server_root
    refresh_remote_paths()

    print(f"[REMOTE ROOT] middlebox={REMOTE_PROJECT_ROOT_MIDDLEBOX}")
    print(f"[REMOTE ROOT] server={REMOTE_PROJECT_ROOT_SERVER}")


def quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def combine_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


def run_local(command: str, *, cwd: Path | None = None, check: bool = True, timeout: int | None = None) -> str:
    print(f"EXEC LOCAL: {command}")
    result = subprocess.run(
        command,
        shell=True,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = combine_output(result)
    if check and result.returncode != 0:
        raise RuntimeError(f"Comando locale fallito ({result.returncode}): {command}\n{output}")
    return output


def run_local_proc(command: list[str], *, cwd: Path | None = None, check: bool = True, timeout: int | None = None) -> str:
    print("EXEC LOCAL PROC:", " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = combine_output(result)
    if check and result.returncode != 0:
        raise RuntimeError(f"Comando locale fallito ({result.returncode}): {' '.join(command)}\n{output}")
    return output


def scp_base_cmd(host: str, *, force_password: bool = False) -> list[str]:
    base = [
        "scp",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=8",
    ]
    if SSH_KEY_PATH.exists():
        base += ["-i", str(SSH_KEY_PATH)]
    if not force_password:
        base += ["-o", "BatchMode=yes"]
    return base


def ssh_base_cmd(host: str, *, force_password: bool = False) -> list[str]:
    base = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=8",
    ]
    if SSH_KEY_PATH.exists():
        base += ["-i", str(SSH_KEY_PATH)]
    if not force_password:
        base += ["-o", "BatchMode=yes"]
    return base + [f"{SSH_USER}@{host}"]


def detect_ssh_mode(host: str) -> str:
    cached = _SSH_MODE_CACHE.get(host)
    if cached:
        return cached

    probe = subprocess.run(
        ssh_base_cmd(host, force_password=False) + ["echo ok"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and "ok" in (probe.stdout or ""):
        _SSH_MODE_CACHE[host] = "key"
        return "key"

    has_sshpass = subprocess.run(
        ["bash", "-lc", "command -v sshpass >/dev/null 2>&1"],
        capture_output=True,
        text=True,
    ).returncode == 0
    if has_sshpass:
        _SSH_MODE_CACHE[host] = "sshpass"
        return "sshpass"

    try:
        import paramiko  # type: ignore

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                username=SSH_USER,
                timeout=8,
                banner_timeout=8,
                auth_timeout=8,
                look_for_keys=False,
                allow_agent=False,
            )
            _SSH_MODE_CACHE[host] = "paramiko"
            return "paramiko"
        finally:
            client.close()
    except ImportError:
        pass
    except Exception:
        pass

    raise RuntimeError(
        f"Impossibile autenticarsi via SSH su {host}. "
        "Nessuna chiave SSH valida, sshpass assente e fallback paramiko non disponibile. "
        "Installa sshpass (sudo apt install sshpass) oppure paramiko (python3 -m pip install --user paramiko)."
    )


def run_ssh_paramiko(host: str, remote_command: str, *, check: bool = True, timeout: int | None = None) -> str:
    try:
        import paramiko  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Modalita paramiko richiesta ma modulo non installato. "
            "Esegui: python3 -m pip install --user paramiko"
        ) from exc

    wrapped_command = f"bash -lc {shlex.quote(remote_command)}"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=SSH_USER,
            timeout=timeout or STARTUP_TIMEOUT,
            banner_timeout=timeout or STARTUP_TIMEOUT,
            auth_timeout=timeout or STARTUP_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        stdin, stdout, stderr = client.exec_command(wrapped_command, timeout=timeout)
        del stdin
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
    finally:
        client.close()

    output = out + err
    if check and exit_code != 0:
        raise RuntimeError(
            f"Comando SSH (paramiko) fallito ({exit_code}) su {host}: {remote_command}\n{output}"
        )
    return output


def run_ssh(host: str, remote_command: str, *, check: bool = True, timeout: int | None = None) -> str:
    mode = detect_ssh_mode(host)
    if mode == "paramiko":
        print(f"EXEC SSH PARAMIKO: {SSH_USER}@{host} <remote_cmd>")
        return run_ssh_paramiko(host, remote_command, check=check, timeout=timeout)

    if mode == "key":
        cmd = ssh_base_cmd(host, force_password=False) + [remote_command]
    else:
        cmd = [
            "sshpass",
            "-p",
            *ssh_base_cmd(host, force_password=True),
            remote_command,
        ]

    print("EXEC SSH:", " ".join(cmd[:-1]), "<remote_cmd>")
    result = subprocess.run(cmd, capture_output=True, text=False, timeout=timeout)
    stdout_text = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    output = stdout_text + stderr_text
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Comando SSH fallito ({result.returncode}) su {host}: {remote_command}\n{output}"
        )
    return output


def upload_file_to_remote(host: str, local_path: Path, remote_path: str) -> None:
    mode = detect_ssh_mode(host)

    if mode == "paramiko":
        try:
            import paramiko  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Paramiko richiesto per upload remoto ma non disponibile") from exc

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                username=SSH_USER,
                timeout=STARTUP_TIMEOUT,
                banner_timeout=STARTUP_TIMEOUT,
                auth_timeout=STARTUP_TIMEOUT,
                look_for_keys=False,
                allow_agent=False,
            )
            sftp = client.open_sftp()
            try:
                sftp.put(str(local_path), remote_path)
            finally:
                sftp.close()
        finally:
            client.close()
        return

    target = f"{SSH_USER}@{host}:{remote_path}"
    if mode == "key":
        cmd = [*scp_base_cmd(host, force_password=False), str(local_path), target]
    else:
        cmd = [
            "sshpass",
            "-p",
            *scp_base_cmd(host, force_password=True),
            str(local_path),
            target,
        ]

    result = subprocess.run(cmd, capture_output=True, text=False, timeout=STARTUP_TIMEOUT)
    stdout_text = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"Upload file fallito verso {host}:{remote_path}\n{stdout_text}{stderr_text}"
        )


def ensure_directories() -> None:
    for directory in (BASE_DIR, ANALYSIS_DIR, RUNTIME_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def reset_output_directories() -> None:
    for directory in (ANALYSIS_DIR, RUNTIME_DIR, BASE_DIR / "RawLogs", BASE_DIR / "Logs", BASE_DIR / "Grafici"):
        if directory.exists():
            shutil.rmtree(directory)
    if CONTAINER_TIMES_LOG.exists():
        CONTAINER_TIMES_LOG.unlink()


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


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


def ensure_server_certificates_remote() -> None:
    local_cert = SERVER_CERTS_HOST_DIR / "cert.pem"
    local_key = SERVER_CERTS_HOST_DIR / "key.pem"
    run_ssh(INDIRIZZO_SERVER, f"mkdir -p {quote(REMOTE_SERVER_CERTS_DIR)}")
    upload_file_to_remote(INDIRIZZO_SERVER, local_cert, f"{REMOTE_SERVER_CERTS_DIR}/cert.pem")
    upload_file_to_remote(INDIRIZZO_SERVER, local_key, f"{REMOTE_SERVER_CERTS_DIR}/key.pem")
    run_ssh(
        INDIRIZZO_SERVER,
        f"chmod 644 {quote(f'{REMOTE_SERVER_CERTS_DIR}/cert.pem')} && "
        f"chmod 600 {quote(f'{REMOTE_SERVER_CERTS_DIR}/key.pem')}",
    )


def ensure_server_script_remote() -> None:
    if not LOCAL_CERTS_SERVER_SCRIPT.exists():
        raise FileNotFoundError(f"Script server certificati mancante: {LOCAL_CERTS_SERVER_SCRIPT}")
    run_ssh(INDIRIZZO_SERVER, f"mkdir -p {quote(REMOTE_SERVER_DIR)}")
    upload_file_to_remote(INDIRIZZO_SERVER, LOCAL_CERTS_SERVER_SCRIPT, REMOTE_CERTS_SERVER_SCRIPT)


def ensure_server_generate_tool_remote() -> None:
    dc_source = PROJECT_ROOT / "DC" / "go" / "src" / "crypto" / "tls" / "generate_delegated_credential.go"
    if not dc_source.exists():
        raise FileNotFoundError(f"Sorgente tool generate DC mancante: {dc_source}")

    LOCAL_GENERATE_TOOL.parent.mkdir(parents=True, exist_ok=True)
    run_local_proc([str(GO_PROFESSOR), "build", "-o", str(LOCAL_GENERATE_TOOL), str(dc_source)])
    run_ssh(INDIRIZZO_SERVER, f"mkdir -p {quote(REMOTE_RUNTIME_DIR_SERVER)}")
    upload_file_to_remote(INDIRIZZO_SERVER, LOCAL_GENERATE_TOOL, REMOTE_GENERATE_TOOL)
    run_ssh(INDIRIZZO_SERVER, f"chmod +x {quote(REMOTE_GENERATE_TOOL)}")


def ensure_go_binaries_local() -> None:
    if not GO_PROFESSOR.exists():
        raise FileNotFoundError(f"Compilatore Go del professore non trovato: {GO_PROFESSOR}")

    sources = [MB_DIR / "client.go", MB_DIR / "middlebox.go", MB_DIR / "middleboxHandler.go", MB_DIR / "messageTypes.go"]
    if any(not src.exists() for src in sources):
        missing = ", ".join(str(src) for src in sources if not src.exists())
        raise FileNotFoundError(f"Sorgenti Go mancanti: {missing}")

    run_local_proc(
        [str(GO_PROFESSOR), "build", "-o", str(CLIENT_BINARY), "client.go"],
        cwd=MB_DIR,
    )

    run_local_proc(
        [
            str(GO_PROFESSOR),
            "build",
            "-o",
            str(MIDDLEBOX_BINARY),
            "middlebox.go",
            "middleboxHandler.go",
            "messageTypes.go",
        ],
        cwd=MB_DIR,
    )


def ensure_go_binaries_middlebox_remote() -> None:
    go_bin = REMOTE_GO_PROFESSOR
    go_src_dir = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/go/src"

    ensure_go_cmd = (
        "set -e; "
        f"GO_BIN={quote(go_bin)}; "
        f"GO_SRC={quote(go_src_dir)}; "
        "if [ -x \"$GO_BIN\" ]; then \n"
        "  \"$GO_BIN\" version; \n"
        "  exit 0; \n"
        "fi; "
        "if command -v cfgo >/dev/null 2>&1; then \n"
        "  mkdir -p \"$(dirname \"$GO_BIN\")\"; \n"
        "  ln -sf \"$(command -v cfgo)\" \"$GO_BIN\"; \n"
        "  \"$GO_BIN\" version; \n"
        "  exit 0; \n"
        "fi; "
        "if [ -d \"$GO_SRC\" ] && command -v go >/dev/null 2>&1; then \n"
        "  (cd \"$GO_SRC\" && ./make.bash); \n"
        "fi; "
        "test -x \"$GO_BIN\"; "
        "\"$GO_BIN\" version"
    )
    check_output = run_ssh(INDIRIZZO_MIDDLEBOX, ensure_go_cmd, check=False)
    if "go version" not in check_output:
        raise RuntimeError(
            "Compilatore Go del professore non disponibile sul middlebox. "
            f"Provati: path fisso {go_bin}, comando cfgo, bootstrap da {go_src_dir}.\n"
            f"Dettagli:\n{check_output}"
        )

    print(f"[REMOTE GO] middlebox using fixed path: {go_bin}")

    run_ssh(INDIRIZZO_MIDDLEBOX, f"mkdir -p {quote(REMOTE_MB_DIR)}")
    upload_file_to_remote(INDIRIZZO_MIDDLEBOX, MIDDLEBOX_BINARY, REMOTE_MIDDLEBOX_BINARY)
    run_ssh(INDIRIZZO_MIDDLEBOX, f"chmod +x {quote(REMOTE_MIDDLEBOX_BINARY)}")


def ensure_remote_layout() -> None:
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"mkdir -p {quote(REMOTE_RUNTIME_DIR_MIDDLEBOX)} {quote(REMOTE_ANALYSIS_DIR_MIDDLEBOX)}",
    )
    run_ssh(
        INDIRIZZO_SERVER,
        f"mkdir -p {quote(REMOTE_RUNTIME_DIR_SERVER)}",
    )


def append_remote(host: str, remote_path: str, text: str) -> None:
    command = (
        f"python3 - <<'PY'\n"
        f"from pathlib import Path\n"
        f"p = Path({remote_path!r})\n"
        f"p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"with p.open('a', encoding='utf-8') as h:\n"
        f"    h.write({text!r})\n"
        f"PY"
    )
    run_ssh(host, command)


def read_remote_file(host: str, remote_path: str) -> str:
    command = (
        f"python3 - <<'PY'\n"
        f"from pathlib import Path\n"
        f"p = Path({remote_path!r})\n"
        f"print(p.read_text(encoding='utf-8', errors='replace') if p.exists() else '', end='')\n"
        f"PY"
    )
    return run_ssh(host, command, check=True)


def sync_remote_logs() -> None:
    MIDDLEBOX_RUNTIME_LOG.write_text(read_remote_file(INDIRIZZO_MIDDLEBOX, REMOTE_MIDDLEBOX_RUNTIME_LOG), encoding="utf-8")
    SERVER_RUNTIME_LOG.write_text(read_remote_file(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG), encoding="utf-8")


def reset_remote_logs() -> None:
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f": > {quote(REMOTE_MIDDLEBOX_RUNTIME_LOG)}; : > {quote(REMOTE_CONTAINER_TIMES_LOG)}",
    )
    run_ssh(
        INDIRIZZO_SERVER,
        f": > {quote(REMOTE_SERVER_RUNTIME_LOG)}; rm -f {quote(REMOTE_SERVER_PIDFILE)}",
        check=False,
    )


def remote_file_size(host: str, remote_path: str) -> int:
    output = run_ssh(
        host,
        f"python3 - <<'PY'\n"
        f"from pathlib import Path\n"
        f"p = Path({remote_path!r})\n"
        f"print(p.stat().st_size if p.exists() else 0)\n"
        f"PY",
    )
    return int(output.strip() or "0")


def remote_log_contains(host: str, remote_path: str, marker: str, start_offset: int = 0) -> bool:
    command = (
        f"python3 - <<'PY'\n"
        f"from pathlib import Path\n"
        f"p = Path({remote_path!r})\n"
        f"txt = ''\n"
        f"if p.exists():\n"
        f"    data = p.read_bytes()\n"
        f"    txt = data[{start_offset}:].decode('utf-8', errors='replace')\n"
        f"print('1' if {marker!r} in txt else '0')\n"
        f"PY"
    )
    return run_ssh(host, command).strip() == "1"


def wait_for_remote_marker(host: str, remote_path: str, marker: str, start_offset: int = 0) -> None:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if remote_log_contains(host, remote_path, marker, start_offset):
            return
        time.sleep(0.5)
    raise TimeoutError(f"Marker non trovato su {host}:{remote_path}: {marker}")


def parse_ordered_times(text: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for line in text.splitlines():
        for match in TIMESTAMP_PATTERN.finditer(line):
            out.append((int(match.group(1)), int(match.group(2))))
    return out


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
    append_text(CLIENT_RUNTIME_LOG, marker)
    append_remote(INDIRIZZO_MIDDLEBOX, REMOTE_MIDDLEBOX_RUNTIME_LOG, marker)
    append_remote(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG, marker)
    append_remote(INDIRIZZO_MIDDLEBOX, REMOTE_CONTAINER_TIMES_LOG, marker)


def write_operation_marker(exp: int, op: int) -> dict[str, int]:
    marker = f"--- OPERAZIONE {op} (esperimento {exp}) ---\n"
    append_text(CLIENT_RUNTIME_LOG, marker)
    append_remote(INDIRIZZO_MIDDLEBOX, REMOTE_MIDDLEBOX_RUNTIME_LOG, marker)
    append_remote(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG, marker)
    sync_remote_logs()
    return {
        "Client": file_size(CLIENT_RUNTIME_LOG),
        "Middlebox": file_size(MIDDLEBOX_RUNTIME_LOG),
        "Server": file_size(SERVER_RUNTIME_LOG),
    }


def append_container_timing_remote(phase: str, timestamp_ns: int) -> None:
    append_remote(
        INDIRIZZO_MIDDLEBOX,
        REMOTE_CONTAINER_TIMES_LOG,
        f"{MIDDLEBOX_CONTAINER} {phase} = {timestamp_ns} ns\n",
    )


def wait_for_expected_timestamps(op: int, start_offsets: dict[str, int]) -> None:
    component_logs = {
        "Client": CLIENT_RUNTIME_LOG,
        "Middlebox": MIDDLEBOX_RUNTIME_LOG,
        "Server": SERVER_RUNTIME_LOG,
    }

    deadline = time.time() + STARTUP_TIMEOUT
    missing: dict[str, set[int]] = {}
    while time.time() < deadline:
        sync_remote_logs()
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
        time.sleep(0.2)

    details = "; ".join(f"{comp}: missing {sorted(values)}" for comp, values in missing.items())
    raise TimeoutError(f"Timestamp attesi non trovati per operazione {op}: {details}")


def wait_until_log_settles(paths: list[Path]) -> None:
    deadline = time.time() + SETTLE_TIMEOUT
    last_sizes: dict[Path, int] = {}
    stable_since: float | None = None
    while time.time() < deadline:
        sync_remote_logs()
        current_sizes = {path: file_size(path) for path in paths}
        if current_sizes == last_sizes:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= SETTLE_STABLE_FOR:
                return
        else:
            stable_since = None
        last_sizes = current_sizes
        time.sleep(SETTLE_INTERVAL)


def ensure_remote_network() -> None:
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker network inspect {quote(NETWORK)} >/dev/null 2>&1 || docker network create {quote(NETWORK)}",
    )


def cleanup_container_remote() -> None:
    run_ssh(INDIRIZZO_MIDDLEBOX, f"docker rm -f {quote(MIDDLEBOX_CONTAINER)} >/dev/null 2>&1 || true", check=False)


def cleanup_kubernetes_leftovers_remote() -> None:
    """Rimuove eventuali residui Kubernetes che occupano la porta host 8443 sul middlebox.
    Serve quando si alternano test Kubernetes (kubectl port-forward) e Docker nello stesso host."""
    cmd = (
        "pkill -f '[k]ubectl.*port-forward.*8443' >/dev/null 2>&1 || true; "
        "pkill -f '[k]ubectl.*port-forward.*middlebox' >/dev/null 2>&1 || true; "
        "sleep 0.3; "
        "ss -ltnp | grep ':8443' || true"
    )
    run_ssh(INDIRIZZO_MIDDLEBOX, cmd, check=False)


def build_images_remote() -> None:
    print(f"!!! BUILD MIDDLEBOX CONTAINER - START : {time.time_ns()}")
    dockerfile_path = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/Middlebox/Dockerfile.middlebox"
    patch_dockerfile_cmd = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f"p = Path({dockerfile_path!r})\n"
        "txt = p.read_text(encoding='utf-8', errors='replace')\n"
        "old = 'RUN GOOS=linux GOARCH=amd64 /root/go/bin/go build -o middlebox middlebox.go middleboxHandler.go messageTypes.go'\n"
        "new = 'RUN if [ -x /root/go/bin/go ]; then GOOS=linux GOARCH=amd64 /root/go/bin/go build -o middlebox middlebox.go middleboxHandler.go messageTypes.go; else echo \"skip build in image: /root/go/bin/go missing\"; fi'\n"
        "if old in txt and new not in txt:\n"
        "    txt = txt.replace(old, new)\n"
        "    p.write_text(txt, encoding='utf-8')\n"
        "PY"
    )
    run_ssh(INDIRIZZO_MIDDLEBOX, patch_dockerfile_cmd)
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"mkdir -p {quote(f'{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/Middlebox')} && "
        f"touch {quote(f'{REMOTE_PROJECT_ROOT_MIDDLEBOX}/DC/Middlebox/jwks.dat')}",
    )
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"cd {quote(REMOTE_PROJECT_ROOT_MIDDLEBOX)} && docker build -f DC/Middlebox/Dockerfile.middlebox -t middlebox .",
        timeout=180,
    )
    print(f"!!! BUILD MIDDLEBOX CONTAINER - END : {time.time_ns()}")


def create_middlebox_container(exp: int) -> None:
    del exp
    # Se prima era in uso la versione Kubernetes, elimina eventuali port-forward rimasti su 8443.
    cleanup_kubernetes_leftovers_remote()

    start_ns = time.time_ns()
    print(f"!!! RUN CONTAINER {MIDDLEBOX_CONTAINER} - START : {start_ns}")
    append_container_timing_remote("START", start_ns)
    remote_shared_host_dir = f"$HOME/{REMOTE_BASE_DIR_MIDDLEBOX}"

    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        " ".join(
            [
                "docker run -d",
                f"--name {quote(MIDDLEBOX_CONTAINER)}",
                f"--network {quote(NETWORK)}",
                "-p 8443:8443",
                f"-v \"{remote_shared_host_dir}\":{quote(SHARED_VOLUME)}",
                quote(MIDDLEBOX_IMAGE),
                "tail -f /dev/null",
            ]
        ),
    )

    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker cp {quote(REMOTE_MIDDLEBOX_BINARY)} {quote(MIDDLEBOX_CONTAINER)}:/app/middlebox",
    )
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote('chmod +x /app/middlebox')}",
    )

    end_ns = time.time_ns()
    print(f"!!! RUN CONTAINER {MIDDLEBOX_CONTAINER} - END : {end_ns}")
    append_container_timing_remote("END", end_ns)
    append_remote(
        INDIRIZZO_MIDDLEBOX,
        REMOTE_MIDDLEBOX_RUNTIME_LOG,
        f"[MB] Container creation time = {end_ns - start_ns} ns\n",
    )


def start_middlebox_remote() -> None:
    start_idx = remote_file_size(INDIRIZZO_MIDDLEBOX, REMOTE_MIDDLEBOX_RUNTIME_LOG)
    command = (
        f"cd /app && "
        f"MB_TARGET_URL={quote(f'https://{SERVER_PUBLIC_IP}:8000')} "
        f"MB_CERT_URL={quote(f'http://{SERVER_PUBLIC_IP}:5000')} "
        f"MB_UPSTREAM_SERVER_NAME={quote('server')} "
        f"./middlebox >> {quote(f'{SHARED_VOLUME}/Runtime/Middlebox.log')} 2>&1"
    )
    start_ns = time.time_ns()
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker exec -d {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote(command)}",
    )
    wait_for_remote_marker(
        INDIRIZZO_MIDDLEBOX,
        REMOTE_MIDDLEBOX_RUNTIME_LOG,
        "[MB] Middlebox listening on :8443",
        start_idx,
    )
    end_ns = time.time_ns()
    append_remote(
        INDIRIZZO_MIDDLEBOX,
        REMOTE_MIDDLEBOX_RUNTIME_LOG,
        f"[MB] Service startup time = {end_ns - start_ns} ns\n",
    )


def stop_middlebox_remote() -> None:
    kill_command = (
        "pkill -INT -f middlebox 2>/dev/null || true; "
        "sleep 0.5; "
        "pkill -TERM -f middlebox 2>/dev/null || true; "
        "sleep 0.5; "
        "pkill -KILL -f middlebox 2>/dev/null || true"
    )
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote(kill_command)}",
        check=False,
    )


def reset_middlebox_delegation_remote() -> None:
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote('pkill -USR1 -f middlebox 2>/dev/null || true')}",
        check=False,
    )
    time.sleep(0.1)
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote('rm -f /certs/dc.cred /certs/dckey.pem 2>/dev/null; true')}",
        check=False,
    )
    verify_cmd = (
        "if [ -f /certs/dc.cred ] || [ -f /certs/dckey.pem ]; then "
        "echo '[MB] Deleghe non eliminate'; exit 1; "
        "fi"
    )
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"docker exec {quote(MIDDLEBOX_CONTAINER)} sh -lc {quote(verify_cmd)}",
    )


def start_server_remote() -> None:
    start_idx = remote_file_size(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG)
    remote_generate_tool_rel = "TreDispositivi/Misure/Runtime/generate_dc_tool"
    stop_cmd = "pkill -f '[c]erts_server.py' >/dev/null 2>&1 || true"
    run_ssh(INDIRIZZO_SERVER, stop_cmd, check=False)

    start_cmd = (
        f"set -e; "
        f"cd {quote(REMOTE_PROJECT_ROOT_SERVER)}; "
        "mkdir -p TreDispositivi/Misure/Runtime; "
        f"GO_TOOL={quote(remote_generate_tool_rel)} "
        "nohup python3 -u PerformanceMeasuring/certs_server.py "
        ">> TreDispositivi/Misure/Runtime/Server.log 2>&1 < /dev/null & "
        "echo $! > TreDispositivi/Misure/server.pid"
    )
    run_ssh(INDIRIZZO_SERVER, start_cmd)
    wait_for_remote_marker(
        INDIRIZZO_SERVER,
        REMOTE_SERVER_RUNTIME_LOG,
        "[SERVER] Application TLS server running on :8000",
        start_idx,
    )


def stop_server_remote() -> None:
    command = (
        f"if [ -f {quote(REMOTE_SERVER_PIDFILE)} ]; then "
        f"pid=$(cat {quote(REMOTE_SERVER_PIDFILE)}); "
        f"kill $pid >/dev/null 2>&1 || true; "
        f"sleep 1; "
        f"kill -9 $pid >/dev/null 2>&1 || true; "
        f"rm -f {quote(REMOTE_SERVER_PIDFILE)}; "
        f"fi; "
        f"pkill -f '[c]erts_server.py' >/dev/null 2>&1 || true"
    )
    run_ssh(INDIRIZZO_SERVER, command, check=False)


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
    result = subprocess.run(cmd, cwd=str(MB_DIR), capture_output=True, text=True, timeout=STARTUP_TIMEOUT)
    output = combine_output(result)
    append_text(CLIENT_RUNTIME_LOG, output)

    has_call = "Calling:" in output
    has_ok = '"status": "ok"' in output
    has_error = "Error during request" in output or "connection refused" in output or "panic:" in output
    if not has_call or not has_ok or has_error:
        raise RuntimeError(f"Richiesta client fallita verso {url}\n{output}")

    return output


def wait_logs_after_operation(op: int, start_offsets: dict[str, int]) -> None:
    wait_for_expected_timestamps(op, start_offsets)
    wait_until_log_settles([CLIENT_RUNTIME_LOG, MIDDLEBOX_RUNTIME_LOG, SERVER_RUNTIME_LOG])
    time.sleep(0.2)


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
                values = averages[op]["times"][key]
                if values:
                    results_file.write(f"t{key} average = {round(statistics.fmean(values))} ns\n")
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


def generate_graphs_like_grafici() -> None:
    grafici = importlib.import_module("Grafici")

    grafici.BASE_DIR = ANALYSIS_DIR
    grafici.OUTPUT_TXT = ANALYSIS_DIR / "Grafici.txt"
    grafici.DIAGNOSTIC_TXT = ANALYSIS_DIR / "DiagnosticaRuntime.txt"
    grafici.OUTPUT_PDF_DIR = ANALYSIS_DIR

    series = grafici.parse_metric_series(RUNTIME_DIR)
    grafici.write_grafici_txt(series, RUNTIME_DIR)
    grafici.write_runtime_diagnostics(series, RUNTIME_DIR)
    grafici.make_plots(series, show=False, save_pdf=True)

    explosion_by_op = grafici.build_timestamp_avg_for_explosion_by_operation(series)
    for op in grafici.OPS:
        grafici.render_t1_t10_explosion_chart(
            timestamp_avg_ns=explosion_by_op.get(op, {}),
            output_path=grafici.OUTPUT_PDF_DIR / f"T1_T10_Explosion_Operazione_{op}.png",
            title=f"Esplosione temporale t1->t10 - Operazione N_{op}",
            alpha=grafici.EXPLOSION_ALPHA,
        )


def stop_experiment_services() -> None:
    stop_middlebox_remote()
    stop_server_remote()
    time.sleep(1)


def run_single_experiment(exp: int) -> None:
    print(f"\n===== ESPERIMENTO {exp} =====\n")
    write_experiment_markers(exp)

    cleanup_container_remote()
    create_middlebox_container(exp)

    try:
        start_server_remote()
        start_middlebox_remote()

        if 1 in OPS:
            print("OPERAZIONE 1")
            offsets = write_operation_marker(exp, 1)
            client_request(f"{MIDDLEBOX_CLIENT_FACING_IP}:8443")
            wait_logs_after_operation(1, offsets)

        if 2 in OPS:
            print("OPERAZIONE 2")
            offsets = write_operation_marker(exp, 2)
            reset_middlebox_delegation_remote()
            client_request(f"{MIDDLEBOX_CLIENT_FACING_IP}:8443")
            wait_logs_after_operation(2, offsets)

        if 3 in OPS:
            print("OPERAZIONE 3")
            offsets = write_operation_marker(exp, 3)
            client_request(f"{MIDDLEBOX_CLIENT_FACING_IP}:8443")
            wait_logs_after_operation(3, offsets)

        if 4 in OPS:
            print("OPERAZIONE 4")
            offsets = write_operation_marker(exp, 4)
            client_request(f"{SERVER_PUBLIC_IP}:8000")
            wait_logs_after_operation(4, offsets)
    finally:
        stop_experiment_services()
        cleanup_container_remote()


def run_experiments() -> None:
    reset_output_directories()
    ensure_directories()
    resolve_remote_paths()
    ensure_remote_layout()
    reset_remote_logs()
    ensure_external_certificates()
    ensure_server_certificates_remote()
    ensure_server_script_remote()
    ensure_server_generate_tool_remote()
    ensure_go_binaries_local()
    ensure_go_binaries_middlebox_remote()
    ensure_remote_network()
    cleanup_container_remote()
    build_images_remote()

    for exp in range(1, N + 1):
        run_single_experiment(exp)

    sync_remote_logs()
    CONTAINER_TIMES_LOG.write_text(
        read_remote_file(INDIRIZZO_MIDDLEBOX, REMOTE_CONTAINER_TIMES_LOG),
        encoding="utf-8",
    )


def emergency_cleanup_remote() -> None:
    try:
        cleanup_kubernetes_leftovers_remote()
    except Exception:
        pass
    try:
        stop_middlebox_remote()
    except Exception:
        pass
    try:
        cleanup_container_remote()
    except Exception:
        pass
    try:
        stop_server_remote()
    except Exception:
        pass


def verify_cleanup_remote() -> bool:
    checks = [
        (INDIRIZZO_SERVER, 5000),
        (INDIRIZZO_SERVER, 8000),
        (INDIRIZZO_MIDDLEBOX, 8443),
    ]
    all_clear = True
    for host, port in checks:
        output = run_ssh(host, f"ss -ltnp | grep :{port} || true", check=False)
        if output.strip():
            print(f"ATTENZIONE: Porta {port} ancora in ascolto su {host}: {output.strip()}")
            all_clear = False
        else:
            print(f"✓ Porta {port} libera su {host}")
    return all_clear


def main() -> None:
    try:
        # run_experiments()
        analyze_logs()
        generate_graphs_like_grafici()
        print("\nEsperimenti completati (orchestrazione SSH)")
        print(f"Runtime: {RUNTIME_DIR}")
        print(f"Analisi e grafici: {ANALYSIS_DIR}")
    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    # finally:
    #     try:
    #         emergency_cleanup_remote()
    #         verify_cleanup_remote()
    #     except Exception as exc:
    #         print(f"Errore durante cleanup remoto: {exc}")


if __name__ == "__main__":
    main()
