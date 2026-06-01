from __future__ import annotations

import importlib
import os
import re
import socket
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path


############################################
# CONFIGURAZIONE
############################################

INDIRIZZO_MIDDLEBOX = "10.79.1.175"
INDIRIZZO_SERVER = "10.79.1.208"

# Topologia client/middlebox/server (come TreMisure.py)
CLIENT_PUBLIC_IP = "10.79.1.165"
CLIENT_MIDDLEBOX_FACING_IP = "172.16.1.2"
MIDDLEBOX_PUBLIC_IP = INDIRIZZO_MIDDLEBOX
MIDDLEBOX_CLIENT_FACING_IP = "172.16.1.1"
MIDDLEBOX_SERVER_FACING_IP = "172.16.0.1"
SERVER_PUBLIC_IP = INDIRIZZO_SERVER
SERVER_MIDDLEBOX_FACING_IP = "172.16.0.2"

SSH_USER = "bonsai"
SSH_KEY_PATH = Path.home() / ".ssh" / "id_ed25519_masterthesis"

N = 50
TYPE = "GET"
OPS = [1, 2, 3]
TOKEN = "token"

# Kubernetes sul nodo middlebox (accesso via SSH)
NAMESPACE = "default"
MIDDLEBOX_POD = "middlebox"
MIDDLEBOX_SVC = "middlebox-svc"
MIDDLEBOX_PORT = 8443
MB_LOG_PATH_IN_POD = "/tmp/Middlebox.log"
MIDDLEBOX_IMAGE = "middlebox"

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT: Path | None = None
for _candidate in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
    if (_candidate / "PerformanceMeasuring").exists() and (_candidate / "DC" / "Middlebox").exists():
        PROJECT_ROOT = _candidate
        break
if PROJECT_ROOT is None:
    PROJECT_ROOT = SCRIPT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVER_DIR = PROJECT_ROOT / "PerformanceMeasuring"
MB_DIR = PROJECT_ROOT / "DC" / "Middlebox"
GO_PROFESSOR = PROJECT_ROOT / "DC" / "go" / "bin" / "go"
CLIENT_BINARY = MB_DIR / "client"
LOCAL_CERTS_SERVER_SCRIPT = SERVER_DIR / "certs_server.py"

# Percorsi remoti (sul server via SSH)
REMOTE_PROJECT_ROOT_SERVER = "MasterThesis"
REMOTE_PROJECT_ROOT_MIDDLEBOX = "MasterThesis"
REMOTE_SERVER_DIR = f"{REMOTE_PROJECT_ROOT_SERVER}/PerformanceMeasuring"
REMOTE_SERVER_CERTS_DIR = f"{REMOTE_PROJECT_ROOT_SERVER}/certs_external/server"
REMOTE_BASE_DIR_SERVER = f"{REMOTE_PROJECT_ROOT_SERVER}/TreDispositivi/MisureKubernetes"
REMOTE_RUNTIME_DIR_SERVER = f"{REMOTE_BASE_DIR_SERVER}/Runtime"
REMOTE_SERVER_RUNTIME_LOG = f"{REMOTE_RUNTIME_DIR_SERVER}/Server.log"
REMOTE_SERVER_PIDFILE = f"{REMOTE_BASE_DIR_SERVER}/server.pid"
REMOTE_GENERATE_TOOL = f"{REMOTE_RUNTIME_DIR_SERVER}/generate_dc_tool"
REMOTE_CERTS_SERVER_SCRIPT = f"{REMOTE_SERVER_DIR}/certs_server.py"

REMOTE_BASE_DIR_MIDDLEBOX = f"{REMOTE_PROJECT_ROOT_MIDDLEBOX}/TreDispositivi/MisureKubernetes"
REMOTE_RUNTIME_DIR_MIDDLEBOX = f"{REMOTE_BASE_DIR_MIDDLEBOX}/Runtime"
REMOTE_MB_PORTFWD_PIDFILE = f"{REMOTE_BASE_DIR_MIDDLEBOX}/middlebox_portforward.pid"
REMOTE_MB_PORTFWD_LOG = f"{REMOTE_RUNTIME_DIR_MIDDLEBOX}/middlebox_portforward.log"

LOCAL_MB_TUNNEL_HOST = "127.0.0.1"
LOCAL_MB_TUNNEL_PORT = 18443
LOCAL_MB_TUNNEL_REMOTE_HOST = "127.0.0.1"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
SERVER_CERTS_HOST_DIR = EXTERNAL_CERTS_DIR / "server"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"

BASE_DIR = PROJECT_ROOT / "TreDispositivi" / "MisureKubernetes"
ANALYSIS_DIR = BASE_DIR / "Analisi"
RUNTIME_DIR = BASE_DIR / "Runtime"

LOCAL_MB_TUNNEL_PIDFILE = BASE_DIR / "middlebox_local_tunnel.pid"
LOCAL_MB_TUNNEL_LOG = RUNTIME_DIR / "middlebox_local_tunnel.log"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_RUNTIME_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"

CONTAINER_TIMES_LOG = BASE_DIR / "Log_container.txt"
LOCAL_GENERATE_TOOL = RUNTIME_DIR / "generate_dc_tool"

STARTUP_TIMEOUT = 60
SETTLE_TIMEOUT = 20
SETTLE_INTERVAL = 0.5
SETTLE_STABLE_FOR = 1.5

_SSH_MODE_CACHE: dict[str, str] = {}
_MIDDLEBOX_NODEPORT_CACHE: int | None = None
_pod_log_offset: int = 0
_LOCAL_TUNNEL_PROC: subprocess.Popen[bytes] | None = None

TIMESTAMP_PATTERN = re.compile(r"\bt(\d+)\b\s*:?\s*[^\n\r]*?=\s*(\d+)")

# Operazione 4 non presente in Kubernetes
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

REQUIRED_TIMESTAMPS = {
    "Client": {1: {10}, 2: {10}, 3: {10}},
    "Middlebox": {1: {2, 3, 40}, 2: {2, 3, 40}, 3: {2, 3, 40}},
    "Server": {1: {25, 26}, 2: {25, 26}, 3: set()},
}


############################################
# UTILITA SSH
############################################

def quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def combine_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


def scp_base_cmd(host: str, *, force_password: bool = False) -> list[str]:
    base = [
        "scp",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
    ]
    if SSH_KEY_PATH.exists():
        base += ["-i", str(SSH_KEY_PATH)]
    if not force_password:
        base += ["-o", "BatchMode=yes"]
    return base


def ssh_base_cmd(host: str, *, force_password: bool = False) -> list[str]:
    base = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
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
            *scp_base_cmd(host, force_password=True),
            str(local_path), target,
        ]

    result = subprocess.run(cmd, capture_output=True, text=False, timeout=STARTUP_TIMEOUT)
    stdout_text = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"Upload file fallito verso {host}:{remote_path}\n{stdout_text}{stderr_text}"
        )


############################################
# UTILITA LOCALI E REMOTE
############################################

def run_local_proc(
    command: list[str], *, cwd: Path | None = None, check: bool = True, timeout: int | None = None
) -> str:
    print("EXEC LOCAL PROC:", " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if check and result.returncode != 0:
        raise RuntimeError(f"Comando locale fallito ({result.returncode}): {' '.join(command)}\n{output}")
    return output


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


def sync_server_log() -> None:
    """Sovrascrive il log server locale con il contenuto aggiornato dal server remoto."""
    SERVER_RUNTIME_LOG.write_text(
        read_remote_file(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG),
        encoding="utf-8",
    )


def reset_remote_server_log() -> None:
    run_ssh(
        INDIRIZZO_SERVER,
        f": > {quote(REMOTE_SERVER_RUNTIME_LOG)}; rm -f {quote(REMOTE_SERVER_PIDFILE)}",
        check=False,
    )


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
        raise FileNotFoundError(f"Script server mancante: {LOCAL_CERTS_SERVER_SCRIPT}")
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


def ensure_client_binary() -> None:
    src = MB_DIR / "client.go"
    if CLIENT_BINARY.exists() and src.exists() and CLIENT_BINARY.stat().st_mtime >= src.stat().st_mtime:
        return
    if not GO_PROFESSOR.exists():
        raise FileNotFoundError(f"Compilatore Go del professore non trovato: {GO_PROFESSOR}")
    run_local_proc(
        [str(GO_PROFESSOR), "build", "-o", str(CLIENT_BINARY), "client.go"],
        cwd=MB_DIR,
    )


def ensure_remote_server_layout() -> None:
    run_ssh(INDIRIZZO_SERVER, f"mkdir -p {quote(REMOTE_RUNTIME_DIR_SERVER)}")


############################################
# KUBERNETES VIA SSH (sul nodo middlebox)
############################################

_KUBECTL_PATH_CACHE: str | None = None
_KUBECONFIG_PATH_CACHE: str | None = None

# Percorsi comuni in cui kubectl può trovarsi su sistemi Linux/snap/k3s/kind
_KUBECTL_SEARCH_PATHS = [
    "/usr/local/bin/kubectl",
    "/usr/bin/kubectl",
    "/snap/bin/kubectl",
    f"/home/{SSH_USER}/.local/bin/kubectl",
    f"/home/{SSH_USER}/bin/kubectl",
    "/opt/homebrew/bin/kubectl",
    "/var/lib/snapd/snap/bin/kubectl",
    "/usr/local/sbin/kubectl",
]

# Wrapper multi-binary (k3s, microk8s) che incorporano kubectl
_KUBECTL_WRAPPERS = [
    ("/usr/local/bin/k3s", "kubectl"),
    ("/usr/bin/k3s", "kubectl"),
    ("/usr/bin/microk8s", "kubectl"),
    ("/snap/bin/microk8s", "kubectl"),
]

_KUBECONFIG_SEARCH_PATHS = [
    f"/home/{SSH_USER}/.kube/config",
    "/etc/rancher/k3s/k3s.yaml",
    "/etc/kubernetes/admin.conf",
    "/var/snap/microk8s/current/credentials/client.config",
]

KIND_CLUSTER_NAME = "masterthesis"
KIND_BIN_INSTALL_PATH = f"/home/{SSH_USER}/.local/bin/kind"


def _kubectl_output_has_cluster_error(output: str) -> bool:
    text = (output or "").lower()
    return (
        "current-context is not set" in text
        or "the connection to the server" in text
        or "no configuration has been provided" in text
        or "could not read kubeconfig" in text
        or ("permission denied" in text and "kube" in text)
    )


def _bootstrap_kubeconfig_remote() -> str | None:
    """Prova a costruire ~/.kube/config usando strumenti cluster presenti sul middlebox.
    Non solleva eccezioni: restituisce il path generato oppure None."""
    target_cfg = f"/home/{SSH_USER}/.kube/config"

    # kind: usa il primo cluster disponibile
    kind_bootstrap = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        (
            "bash -lc 'set -e; "
            f"mkdir -p /home/{SSH_USER}/.kube; "
            "if command -v kind >/dev/null 2>&1; then "
            "  CL=$(kind get clusters 2>/dev/null | head -n1 || true); "
            "  if [ -n \"$CL\" ]; then "
            f"    kind get kubeconfig --name \"$CL\" > {shlex.quote(target_cfg)}; "
            f"    chmod 600 {shlex.quote(target_cfg)}; "
            f"    echo {shlex.quote(target_cfg)}; "
            "  fi; "
            "fi'"
        ),
        check=False,
    ).strip()
    if kind_bootstrap and "/" in kind_bootstrap:
        print(f"[K8S] kubeconfig bootstrap da kind: {kind_bootstrap}")
        return kind_bootstrap.splitlines()[0].strip()

    # microk8s: esporta config utente
    micro_bootstrap = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        (
            "bash -lc 'set -e; "
            f"mkdir -p /home/{SSH_USER}/.kube; "
            "if command -v microk8s >/dev/null 2>&1; then "
            f"  microk8s config > {shlex.quote(target_cfg)} 2>/dev/null || true; "
            f"  [ -s {shlex.quote(target_cfg)} ] && chmod 600 {shlex.quote(target_cfg)} && echo {shlex.quote(target_cfg)}; "
            "fi'"
        ),
        check=False,
    ).strip()
    if micro_bootstrap and "/" in micro_bootstrap:
        print(f"[K8S] kubeconfig bootstrap da microk8s: {micro_bootstrap}")
        return micro_bootstrap.splitlines()[0].strip()

    # k3s: prova prima senza sudo, poi con sudo non interattivo
    k3s_bootstrap = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        (
            "bash -lc 'set -e; "
            f"mkdir -p /home/{SSH_USER}/.kube; "
            "if [ -r /etc/rancher/k3s/k3s.yaml ]; then "
            f"  cp /etc/rancher/k3s/k3s.yaml {shlex.quote(target_cfg)}; "
            f"  chmod 600 {shlex.quote(target_cfg)}; "
            f"  echo {shlex.quote(target_cfg)}; "
            "elif sudo -n test -r /etc/rancher/k3s/k3s.yaml 2>/dev/null; then "
            f"  sudo -n cp /etc/rancher/k3s/k3s.yaml {shlex.quote(target_cfg)}; "
            f"  sudo -n chown {SSH_USER}:{SSH_USER} {shlex.quote(target_cfg)}; "
            f"  chmod 600 {shlex.quote(target_cfg)}; "
            f"  echo {shlex.quote(target_cfg)}; "
            "fi'"
        ),
        check=False,
    ).strip()
    if k3s_bootstrap and "/" in k3s_bootstrap:
        print(f"[K8S] kubeconfig bootstrap da k3s: {k3s_bootstrap}")
        return k3s_bootstrap.splitlines()[0].strip()

    return None


def _check_kubectl_wrapper_remote() -> str | None:
    """Controlla se k3s o microk8s sono disponibili come wrapper per kubectl."""
    for binary, sub in _KUBECTL_WRAPPERS:
        probe = run_ssh(
            INDIRIZZO_MIDDLEBOX,
            f"[ -x {shlex.quote(binary)} ] && {shlex.quote(binary)} {sub} version --client --short 2>/dev/null && echo {shlex.quote(binary)} || true",
            check=False,
        ).strip()
        if probe and probe.endswith(binary):
            return f"{binary} {sub}"
    # Prova anche tramite login shell
    for wrapper in ("k3s kubectl", "microk8s kubectl"):
        probe = run_ssh(
            INDIRIZZO_MIDDLEBOX,
            f"bash -lc {shlex.quote(f'command -v {wrapper.split()[0]} 2>/dev/null && {wrapper} version --client --short 2>/dev/null && echo OK || true')}",
            check=False,
        )
        if "OK" in probe:
            bin_path = run_ssh(
                INDIRIZZO_MIDDLEBOX,
                f"bash -lc {shlex.quote(f'command -v {wrapper.split()[0]}')}",
                check=False,
            ).strip()
            if bin_path:
                return f"{bin_path} {wrapper.split()[1]}"
    return None


def _install_kubectl_remote() -> str:
    """Scarica e installa il binario statico kubectl in ~/.local/bin sul middlebox remoto."""
    install_dir = f"/home/{SSH_USER}/.local/bin"
    install_path = f"{install_dir}/kubectl"

    print(f"[K8S] kubectl non trovato sul middlebox, download automatico in {install_dir} ...")
    install_cmd = (
        "set -e; "
        f"mkdir -p {shlex.quote(install_dir)}; "
        "ARCH=$(uname -m); "
        "case $ARCH in x86_64) ARCH=amd64;; aarch64) ARCH=arm64;; armv7l) ARCH=arm;; esac; "
        "K8S_VER=$(curl -fsSL --connect-timeout 10 https://dl.k8s.io/release/stable.txt 2>/dev/null "
        "  || wget -qO- https://dl.k8s.io/release/stable.txt 2>/dev/null "
        "  || echo v1.30.0); "
        f'DL_URL="https://dl.k8s.io/release/${{K8S_VER}}/bin/linux/${{ARCH}}/kubectl"; '
        f"curl -fsSL --connect-timeout 30 \"$DL_URL\" -o {shlex.quote(install_path)} "
        f"  || wget -qO {shlex.quote(install_path)} \"$DL_URL\"; "
        f"chmod +x {shlex.quote(install_path)}; "
        f"echo {shlex.quote(install_path)}"
    )
    output = run_ssh(INDIRIZZO_MIDDLEBOX, install_cmd, check=True, timeout=120).strip()
    # L'ultimo echo restituisce il path; verifichiamo che sia eseguibile
    candidate = output.splitlines()[-1].strip()
    ok = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"[ -x {shlex.quote(candidate)} ] && echo ok || echo no",
        check=False,
    ).strip()
    if ok == "ok":
        print(f"[K8S] kubectl installato in: {candidate}")
        return candidate
    raise RuntimeError(
        f"Download kubectl fallito sul middlebox {INDIRIZZO_MIDDLEBOX}.\n"
        "Assicurati che il nodo abbia accesso a internet oppure installa kubectl manualmente:\n"
        "  sudo snap install kubectl --classic\n"
        "  oppure: sudo apt install kubectl"
    )


def discover_kubectl_path() -> str:
    """Trova (o installa) il percorso assoluto di kubectl sul nodo middlebox remoto.
    Salva il risultato in cache per non ripetere la ricerca ad ogni chiamata."""
    global _KUBECTL_PATH_CACHE
    if _KUBECTL_PATH_CACHE:
        return _KUBECTL_PATH_CACHE

    # 1. Shell di login: carica .bashrc/.profile, trova kubectl da snap/pip/k3s
    interactive_probe = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        "bash -lc 'command -v kubectl 2>/dev/null || true'",
        check=False,
    ).strip()
    if interactive_probe and not interactive_probe.startswith("bash:") and "/" in interactive_probe:
        _KUBECTL_PATH_CACHE = interactive_probe
        print(f"[K8S] kubectl trovato (login shell): {_KUBECTL_PATH_CACHE}")
        return _KUBECTL_PATH_CACHE

    # 2. Scansione path noti
    search_cmd = " || ".join(
        f"[ -x {shlex.quote(p)} ] && echo {shlex.quote(p)}"
        for p in _KUBECTL_SEARCH_PATHS
    ) + " || true"
    found = run_ssh(INDIRIZZO_MIDDLEBOX, search_cmd, check=False).strip()
    if found and "/" in found:
        _KUBECTL_PATH_CACHE = found.splitlines()[0].strip()
        print(f"[K8S] kubectl trovato (path scan): {_KUBECTL_PATH_CACHE}")
        return _KUBECTL_PATH_CACHE

    # 3. find brute-force
    find_output = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        "find /usr /snap /opt /home -maxdepth 6 -name kubectl -type f -executable 2>/dev/null | head -1 || true",
        check=False,
    ).strip()
    if find_output and "/" in find_output:
        _KUBECTL_PATH_CACHE = find_output.splitlines()[0].strip()
        print(f"[K8S] kubectl trovato (find): {_KUBECTL_PATH_CACHE}")
        return _KUBECTL_PATH_CACHE

    # 4. Wrapper k3s / microk8s
    wrapper = _check_kubectl_wrapper_remote()
    if wrapper:
        _KUBECTL_PATH_CACHE = wrapper
        print(f"[K8S] kubectl disponibile tramite wrapper: {_KUBECTL_PATH_CACHE}")
        return _KUBECTL_PATH_CACHE

    # 5. Download automatico del binario statico
    installed = _install_kubectl_remote()
    _KUBECTL_PATH_CACHE = installed
    return _KUBECTL_PATH_CACHE


def _ensure_kind_binary_remote() -> str:
    """Trova o installa kind sul middlebox remoto (scope utente)."""
    probe = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        "bash -lc 'command -v kind 2>/dev/null || true'",
        check=False,
    ).strip()
    if probe and "/" in probe:
        return probe.splitlines()[0].strip()

    install_dir = f"/home/{SSH_USER}/.local/bin"
    install_path = KIND_BIN_INSTALL_PATH
    install_cmd = (
        "set -e; "
        f"mkdir -p {shlex.quote(install_dir)}; "
        "ARCH=$(uname -m); "
        "case $ARCH in x86_64) ARCH=amd64;; aarch64) ARCH=arm64;; armv7l) ARCH=arm;; esac; "
        "VER=v0.24.0; "
        f'DL_URL="https://kind.sigs.k8s.io/dl/${{VER}}/kind-linux-${{ARCH}}"; '
        f"curl -fsSL --connect-timeout 30 \"$DL_URL\" -o {shlex.quote(install_path)} "
        f"  || wget -qO {shlex.quote(install_path)} \"$DL_URL\"; "
        f"chmod +x {shlex.quote(install_path)}; "
        f"echo {shlex.quote(install_path)}"
    )
    out = run_ssh(INDIRIZZO_MIDDLEBOX, install_cmd, check=True, timeout=120).strip()
    candidate = out.splitlines()[-1].strip() if out else install_path
    ok = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"[ -x {shlex.quote(candidate)} ] && echo ok || echo no",
        check=False,
    ).strip()
    if ok == "ok":
        print(f"[K8S] kind installato in: {candidate}")
        return candidate
    raise RuntimeError("Installazione automatica di kind fallita sul middlebox remoto")


def _ensure_kind_cluster_remote() -> str:
    """Assicura un cluster kind e prepara ~/.kube/config sul middlebox remoto."""
    kind_bin = _ensure_kind_binary_remote()

    docker_probe = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        "docker info >/dev/null 2>&1 && echo ok || echo no",
        check=False,
    ).strip()
    if docker_probe != "ok":
        raise RuntimeError(
            "Docker non disponibile sul middlebox: impossibile creare automaticamente cluster kind."
        )

    exists = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        (
            "bash -lc '"
            f"{shlex.quote(kind_bin)} get clusters 2>/dev/null | "
            f"grep -Fx {shlex.quote(KIND_CLUSTER_NAME)} >/dev/null && echo yes || echo no'"
        ),
        check=False,
    ).strip()
    if exists != "yes":
        print(f"[K8S] Creo cluster kind {KIND_CLUSTER_NAME} sul middlebox...")
        run_ssh(
            INDIRIZZO_MIDDLEBOX,
            (
                "bash -lc 'set -e; "
                f"KIND_EXPERIMENTAL_PROVIDER=docker {shlex.quote(kind_bin)} create cluster "
                f"--name {shlex.quote(KIND_CLUSTER_NAME)} --wait 120s'"
            ),
            check=True,
            timeout=300,
        )

    target_cfg = f"/home/{SSH_USER}/.kube/config"
    cfg_out = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        (
            "bash -lc 'set -e; "
            f"mkdir -p /home/{SSH_USER}/.kube; "
            f"{shlex.quote(kind_bin)} get kubeconfig --name {shlex.quote(KIND_CLUSTER_NAME)} "
            f"> {shlex.quote(target_cfg)}; "
            f"chmod 600 {shlex.quote(target_cfg)}; "
            f"echo {shlex.quote(target_cfg)}'"
        ),
        check=True,
    ).strip()
    cfg_path = cfg_out.splitlines()[-1].strip() if cfg_out else target_cfg
    print(f"[K8S] kubeconfig preparato da kind: {cfg_path}")
    return cfg_path


def discover_kubeconfig_path() -> str | None:
    """Trova un kubeconfig leggibile sul middlebox remoto.
    Restituisce None se non disponibile."""
    global _KUBECONFIG_PATH_CACHE
    if _KUBECONFIG_PATH_CACHE is not None:
        return _KUBECONFIG_PATH_CACHE or None

    # 1) KUBECONFIG esportato nella login shell
    env_probe = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        "bash -lc 'for p in ${KUBECONFIG//:/ }; do [ -r \"$p\" ] && echo \"$p\" && break; done'",
        check=False,
    ).strip()
    if env_probe and "/" in env_probe:
        _KUBECONFIG_PATH_CACHE = env_probe.splitlines()[0].strip()
        print(f"[K8S] kubeconfig trovato (env): {_KUBECONFIG_PATH_CACHE}")
        return _KUBECONFIG_PATH_CACHE

    # 2) Percorsi standard leggibili
    probe_cmd = " || ".join(
        f"[ -r {shlex.quote(path)} ] && echo {shlex.quote(path)}"
        for path in _KUBECONFIG_SEARCH_PATHS
    ) + " || true"
    found = run_ssh(INDIRIZZO_MIDDLEBOX, probe_cmd, check=False).strip()
    if found and "/" in found:
        _KUBECONFIG_PATH_CACHE = found.splitlines()[0].strip()
        print(f"[K8S] kubeconfig trovato: {_KUBECONFIG_PATH_CACHE}")
        return _KUBECONFIG_PATH_CACHE

    # 3) Prova bootstrap da cluster manager locali (kind/microk8s/k3s)
    bootstrapped = _bootstrap_kubeconfig_remote()
    if bootstrapped:
        _KUBECONFIG_PATH_CACHE = bootstrapped
        return _KUBECONFIG_PATH_CACHE

    # 4) Tentativo best-effort: copia kubeconfig k3s con sudo non-interattivo
    copied = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        (
            "bash -lc 'set -e; "
            f"mkdir -p /home/{SSH_USER}/.kube; "
            "if sudo -n test -r /etc/rancher/k3s/k3s.yaml 2>/dev/null; then "
            f"  sudo -n cp /etc/rancher/k3s/k3s.yaml /home/{SSH_USER}/.kube/config; "
            f"  sudo -n chown {SSH_USER}:{SSH_USER} /home/{SSH_USER}/.kube/config; "
            f"  chmod 600 /home/{SSH_USER}/.kube/config; "
            f"  echo /home/{SSH_USER}/.kube/config; "
            "fi'"
        ),
        check=False,
    ).strip()
    if copied and "/" in copied:
        _KUBECONFIG_PATH_CACHE = copied.splitlines()[0].strip()
        print(f"[K8S] kubeconfig preparato con sudo: {_KUBECONFIG_PATH_CACHE}")
        return _KUBECONFIG_PATH_CACHE

    _KUBECONFIG_PATH_CACHE = ""
    return None


def run_kubectl(kube_cmd: str, *, check: bool = True, timeout: int | None = None) -> str:
    """Esegue un comando kubectl sul nodo middlebox via SSH usando il path assoluto."""
    kubectl_bin = discover_kubectl_path()
    kubeconfig = discover_kubeconfig_path()
    if kubeconfig:
        cmd = f"KUBECONFIG={quote(kubeconfig)} {kubectl_bin} {kube_cmd}"
    else:
        cmd = f"{kubectl_bin} {kube_cmd}"
    return run_ssh(INDIRIZZO_MIDDLEBOX, cmd, check=check, timeout=timeout)


def kubectl_apply_yaml(yaml_content: str) -> None:
    """Applica un manifest YAML via kubectl sul nodo middlebox via SSH.
    Usa pipe da heredoc a kubectl apply -f - per evitare problemi con file temporanei
    e per supportare wrapper come 'k3s kubectl' (path a due parole)."""
    kubectl_bin = discover_kubectl_path()
    # Heredoc shell: il YAML viene passato via stdin a kubectl
    # Il delimitatore non compare mai in un manifest Kubernetes standard
    cmd = f"cat <<'__YAML_MANIFEST_EOF__' | {kubectl_bin} apply -f -\n{yaml_content}\n__YAML_MANIFEST_EOF__"
    run_ssh(INDIRIZZO_MIDDLEBOX, cmd)


def ensure_kubernetes_cluster_ready() -> None:
    global _KUBECONFIG_PATH_CACHE

    current_context = run_kubectl("config current-context", check=False).strip()
    if (not current_context) or current_context.lower().startswith("error:"):
        available_raw = run_kubectl("config get-contexts -o name", check=False)
        contexts = [line.strip() for line in available_raw.splitlines() if line.strip() and not line.lower().startswith("error:")]
        if contexts:
            run_kubectl(f"config use-context {quote(contexts[0])}", check=False)
            current_context = run_kubectl("config current-context", check=False).strip()

    if (not current_context) or current_context.lower().startswith("error:"):
        available = run_kubectl("config get-contexts -o name", check=False).strip()
        kubeconfig = discover_kubeconfig_path()
        raise RuntimeError(
            f"Nessun Kubernetes context attivo su {INDIRIZZO_MIDDLEBOX}.\n"
            f"KUBECONFIG rilevato: {kubeconfig or '(assente)'}\n"
            f"Context disponibili: {available or '(nessuno)'}\n"
            "Configura kubeconfig/context sul middlebox (es. ~/.kube/config o /etc/rancher/k3s/k3s.yaml)."
        )
    api_probe = run_kubectl("--request-timeout=5s get --raw=/readyz", check=False)
    if "ok" not in api_probe.lower():
        raise RuntimeError(
            f"API server Kubernetes non raggiungibile su {INDIRIZZO_MIDDLEBOX} "
            f"(contesto: {current_context}).\n{api_probe.strip()}"
        )
    print(f"[K8S] Cluster Kubernetes raggiungibile su {INDIRIZZO_MIDDLEBOX} (context: {current_context})")


def ensure_middlebox_nodeport_service() -> None:
    """Crea o aggiorna il NodePort service per il pod middlebox sul cluster remoto."""
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
    kubectl_apply_yaml(svc_yaml)


def ensure_server_cluster_service() -> None:
    """Crea Service + Endpoints Kubernetes sul cluster middlebox per raggiungere il server remoto."""
    manifest = (
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: server\n"
        f"  namespace: {NAMESPACE}\n"
        "spec:\n"
        "  ports:\n"
        "    - name: certs\n"
        "      port: 5000\n"
        "      protocol: TCP\n"
        "    - name: app-tls\n"
        "      port: 8000\n"
        "      protocol: TCP\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Endpoints\n"
        "metadata:\n"
        "  name: server\n"
        f"  namespace: {NAMESPACE}\n"
        "subsets:\n"
        "  - addresses:\n"
        f"    - ip: {INDIRIZZO_SERVER}\n"
        "    ports:\n"
        "    - name: certs\n"
        "      port: 5000\n"
        "      protocol: TCP\n"
        "    - name: app-tls\n"
        "      port: 8000\n"
        "      protocol: TCP\n"
    )
    kubectl_apply_yaml(manifest)


def get_middlebox_nodeport() -> int:
    global _MIDDLEBOX_NODEPORT_CACHE
    if _MIDDLEBOX_NODEPORT_CACHE is not None:
        return _MIDDLEBOX_NODEPORT_CACHE

    deadline = time.time() + 30
    while time.time() < deadline:
        output = run_kubectl(
            f"-n {quote(NAMESPACE)} get svc {quote(MIDDLEBOX_SVC)} "
            "-o jsonpath='{.spec.ports[0].nodePort}'",
            check=False,
        ).strip().strip("'")
        if output:
            try:
                _MIDDLEBOX_NODEPORT_CACHE = int(output)
                print(f"[K8S] NodePort middlebox: {_MIDDLEBOX_NODEPORT_CACHE}")
                return _MIDDLEBOX_NODEPORT_CACHE
            except ValueError:
                pass
        time.sleep(1)

    raise RuntimeError(
        f"Impossibile ottenere NodePort del service {MIDDLEBOX_SVC} dopo 30 secondi."
    )


def get_pod_log_content_raw(start_offset: int = 0) -> str:
    """Legge il log del pod middlebox dal cluster remoto, opzionalmente da un byte offset."""
    if start_offset <= 0:
        pod_command = f"cat {quote(MB_LOG_PATH_IN_POD)} 2>/dev/null || true"
    else:
        pod_command = f"tail -c +{start_offset + 1} {quote(MB_LOG_PATH_IN_POD)} 2>/dev/null || true"
    return run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- sh -c {quote(pod_command)}",
        check=False,
    )


def get_pod_log_size() -> int:
    output = run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -c {quote(f'wc -c < {quote(MB_LOG_PATH_IN_POD)} 2>/dev/null || echo 0')}",
        check=False,
    ).strip()
    try:
        return int(output)
    except ValueError:
        return 0


def wait_for_marker_in_pod(marker: str, start_offset: int = 0) -> None:
    """Attende che il marker appaia nel log del pod (letto via SSH+kubectl exec)."""
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if marker in get_pod_log_content_raw(start_offset):
            return
        time.sleep(0.5)
    raise TimeoutError(f"Marker non trovato nel log del pod {MIDDLEBOX_POD}: {marker!r}")


def sync_middlebox_log_increment() -> None:
    """Appende al log middlebox locale il nuovo contenuto del log del pod (dall'offset corrente)."""
    global _pod_log_offset
    new_content = get_pod_log_content_raw(_pod_log_offset)
    if new_content:
        append_text(MIDDLEBOX_RUNTIME_LOG, new_content)
        _pod_log_offset += len(new_content.encode("utf-8"))


def sync_remote_logs() -> None:
    """Sincronizza i log remoti in locale:
    - Server: sovrascrittura completa dal server remoto
    - Middlebox: append incrementale dal log del pod
    """
    sync_server_log()
    sync_middlebox_log_increment()


def pod_exists() -> bool:
    probe = run_kubectl(
        f"-n {quote(NAMESPACE)} get pod {quote(MIDDLEBOX_POD)} --ignore-not-found",
        check=False,
    )
    if _kubectl_output_has_cluster_error(probe):
        return False
    lines = [line for line in probe.splitlines() if line.strip()]
    return len(lines) > 1


def kubernetes_api_reachable() -> bool:
    probe = run_kubectl("--request-timeout=3s get --raw=/readyz", check=False)
    return "ok" in probe.lower()


def delete_middlebox_pod() -> None:
    run_kubectl(
        f"-n {quote(NAMESPACE)} delete pod {quote(MIDDLEBOX_POD)} "
        "--ignore-not-found=true --wait=false",
        check=False,
        timeout=30,
    )

    deadline = time.time() + 120
    while time.time() < deadline:
        if not pod_exists():
            return
        time.sleep(1)

    # Pod bloccato in Terminating: forza l'eliminazione
    run_kubectl(
        f"-n {quote(NAMESPACE)} delete pod {quote(MIDDLEBOX_POD)} "
        "--ignore-not-found=true --grace-period=0 --force",
        check=False,
        timeout=30,
    )

    force_deadline = time.time() + 30
    while time.time() < force_deadline:
        if not pod_exists():
            return
        time.sleep(1)

    raise RuntimeError(
        f"Il pod {MIDDLEBOX_POD} non è stato eliminato. "
        "Verifica eventuali finalizer o risorse bloccate nel cluster."
    )


def create_middlebox_pod(exp: int) -> None:
    """Crea il pod middlebox sul cluster remoto e misura il tempo di creazione."""
    global _pod_log_offset
    _pod_log_offset = 0

    start_ns = time.time_ns()
    print(f"!!! CREATE POD {MIDDLEBOX_POD} - START : {start_ns}")
    append_text(CONTAINER_TIMES_LOG, f"{MIDDLEBOX_POD} START = {start_ns} ns\n")

    run_kubectl(
        f"-n {quote(NAMESPACE)} run {quote(MIDDLEBOX_POD)} "
        f"--image={quote(MIDDLEBOX_IMAGE)} "
        "--restart=Never "
        "--image-pull-policy=Never "
        "-- tail -f /dev/null"
    )

    try:
        run_kubectl(
            f"-n {quote(NAMESPACE)} wait --for=condition=Ready "
            f"pod/{quote(MIDDLEBOX_POD)} --timeout=120s"
        )
    except Exception as exc:
        pod_status = run_kubectl(
            f"-n {quote(NAMESPACE)} get pod {quote(MIDDLEBOX_POD)} -o wide",
            check=False,
        )
        pod_desc = run_kubectl(
            f"-n {quote(NAMESPACE)} describe pod {quote(MIDDLEBOX_POD)}",
            check=False,
        )
        recent_events = run_kubectl(
            f"-n {quote(NAMESPACE)} get events --sort-by=.lastTimestamp | tail -n 20",
            check=False,
        )
        raise RuntimeError(
            f"Pod {MIDDLEBOX_POD} non Ready entro timeout.\n"
            f"Stato pod:\n{pod_status}\n"
            f"Describe pod:\n{pod_desc}\n"
            f"Eventi recenti:\n{recent_events}"
        ) from exc

    end_ns = time.time_ns()
    print(f"!!! CREATE POD {MIDDLEBOX_POD} - END : {end_ns}")
    append_text(CONTAINER_TIMES_LOG, f"{MIDDLEBOX_POD} END = {end_ns} ns\n")

    pod_creation_time_ns = end_ns - start_ns
    append_text(MIDDLEBOX_RUNTIME_LOG, f"[MB] Pod creation time = {pod_creation_time_ns} ns\n")

    # Inizializza il file di log dentro il pod
    run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
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
    run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(kill_command)}",
        check=False,
    )


def compile_middlebox_on_host_and_copy_to_pod() -> str:
    """Compila middlebox sul nodo host 10.79.1.175 e copia il binario nel pod.
    Usato quando nel pod manca /app/middlebox e non c'e toolchain Go interna."""
    remote_project_root_abs = f"/home/{SSH_USER}/{REMOTE_PROJECT_ROOT_MIDDLEBOX}"
    remote_mb_dir = f"{remote_project_root_abs}/DC/Middlebox"
    remote_runtime_dir = f"{remote_project_root_abs}/TreDispositivi/MisureKubernetes/Runtime"
    remote_bin = f"{remote_runtime_dir}/middlebox_from_host"

    compile_cmd = (
        "set -e; "
        f"mkdir -p {quote(remote_runtime_dir)}; "
        f"cd {quote(remote_mb_dir)}; "
        "GO_BIN=''; "
        "if command -v cfgo >/dev/null 2>&1; then GO_BIN=cfgo; "
        f"elif [ -x {quote(f'/home/{SSH_USER}/MasterThesis/DC/go/bin/go')} ]; then GO_BIN={quote(f'/home/{SSH_USER}/MasterThesis/DC/go/bin/go')}; "
        "elif command -v go >/dev/null 2>&1; then GO_BIN=go; "
        "fi; "
        "if [ -z \"$GO_BIN\" ]; then echo GO_MISSING_HOST; exit 1; fi; "
        "$GO_BIN build -o " + quote(remote_bin) + " middlebox.go middleboxHandler.go messageTypes.go; "
        f"chmod +x {quote(remote_bin)}; "
        "echo BUILT_WITH:$GO_BIN"
    )
    compile_out = run_ssh(INDIRIZZO_MIDDLEBOX, f"bash -lc {quote(compile_cmd)}", check=False, timeout=600)
    if "BUILT_WITH:" not in compile_out:
        raise RuntimeError(
            "Compilazione middlebox sul nodo host fallita.\n"
            f"Dettagli:\n{compile_out}"
        )

    exists_out = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"[ -f {quote(remote_bin)} ] && ls -l {quote(remote_bin)} || echo BIN_MISSING_HOST",
        check=False,
    )
    if "BIN_MISSING_HOST" in exists_out:
        raise RuntimeError(
            "Compilazione host riportata come completata ma il binario non esiste nel path atteso.\n"
            f"Path: {remote_bin}\n"
            f"Dettagli:\n{compile_out}"
        )

    # Canonicalizza il path assoluto sul middlebox per evitare ambiguita di cwd
    remote_bin_abs = run_ssh(
        INDIRIZZO_MIDDLEBOX,
        f"python3 - <<'PY'\nfrom pathlib import Path\np=Path({remote_bin!r})\nprint(p.resolve())\nPY",
        check=True,
    ).strip().splitlines()[-1].strip()

    kubectl_bin = discover_kubectl_path()
    kubeconfig = discover_kubeconfig_path()
    if kubeconfig:
        kubectl_cp_cmd = (
            f"{kubectl_bin} --kubeconfig {quote(kubeconfig)} -n {quote(NAMESPACE)} cp "
            f"{quote(remote_bin_abs)} {quote(f'{MIDDLEBOX_POD}:/app/middlebox')}"
        )
        kubectl_chmod_cmd = (
            f"{kubectl_bin} --kubeconfig {quote(kubeconfig)} -n {quote(NAMESPACE)} exec "
            f"{quote(MIDDLEBOX_POD)} -- sh -lc {quote('chmod +x /app/middlebox && ls -l /app/middlebox')}"
        )
    else:
        kubectl_cp_cmd = (
            f"{kubectl_bin} -n {quote(NAMESPACE)} cp "
            f"{quote(remote_bin_abs)} {quote(f'{MIDDLEBOX_POD}:/app/middlebox')}"
        )
        kubectl_chmod_cmd = (
            f"{kubectl_bin} -n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
            f"sh -lc {quote('chmod +x /app/middlebox && ls -l /app/middlebox')}"
        )

    copy_cmd = f"{kubectl_cp_cmd} && {kubectl_chmod_cmd}"
    copy_out = run_ssh(INDIRIZZO_MIDDLEBOX, copy_cmd, check=False, timeout=180)
    if "/app/middlebox" not in copy_out:
        raise RuntimeError(
            "Copia binario middlebox host->pod fallita.\n"
            f"Path host usato: {remote_bin_abs}\n"
            f"Dettagli compile:\n{compile_out}\n"
            f"Dettagli copy:\n{copy_out}"
        )
    return f"{compile_out}\n{exists_out}\n{copy_out}"


def start_middlebox_in_pod() -> None:
    """Avvia il processo middlebox dentro il pod remoto e attende che sia pronto."""
    current_size = get_pod_log_size()

    binary_check = run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        "sh -lc '[ -x /app/middlebox ] && echo OK || (echo MISSING; ls -la /app)';",
        check=False,
    )
    if "OK" not in binary_check:
        # Fallback Kubernetes puro: compila nel pod se i sorgenti sono presenti
        build_output = run_kubectl(
            f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
            "sh -lc 'cd /app && "
            "if command -v go >/dev/null 2>&1; then "
            "  go build -o /app/middlebox . >/tmp/mb_build.log 2>&1 || (cat /tmp/mb_build.log; exit 1); "
            "  chmod +x /app/middlebox; echo BUILT; "
            "else "
            "  echo GO_MISSING; "
            "fi'",
            check=False,
        )
        if "BUILT" not in build_output:
            # Fallback: compila sul nodo host middlebox (via SSH) e copia nel pod.
            try:
                host_fallback_out = compile_middlebox_on_host_and_copy_to_pod()
            except Exception as exc:
                raise RuntimeError(
                    "Nel pod middlebox manca il binario /app/middlebox e non è stato possibile compilarlo nel pod.\n"
                    "Inoltre è fallita anche la compilazione/copia dal nodo host middlebox.\n"
                    f"Dettagli check:\n{binary_check}\n"
                    f"Dettagli build pod:\n{build_output}\n"
                    f"Errore fallback host:\n{exc}"
                ) from exc
            print(f"[MB] Fallback host->pod completato:\n{host_fallback_out}")

    inner_cmd = (
        "cd /app && "
        # Allineato a TreMisure.py: upstream diretto verso IP server remoto.
        f"MB_TARGET_URL={shlex.quote(f'https://{SERVER_PUBLIC_IP}:8000')} "
        f"MB_CERT_URL={shlex.quote(f'http://{SERVER_PUBLIC_IP}:5000')} "
        f"MB_UPSTREAM_SERVER_NAME={shlex.quote('server')} "
        f"./middlebox >> {MB_LOG_PATH_IN_POD} 2>&1 &"
    )
    run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(inner_cmd)}"
    )
    wait_for_marker_in_pod("[MB] Middlebox listening on :8443", start_offset=current_size)

    startup_slice = get_pod_log_content_raw(current_size)
    if "address already in use" in startup_slice:
        raise RuntimeError(
            f"Avvio middlebox nel pod fallito: porta già in uso.\n{startup_slice}"
        )


def reset_middlebox_delegation() -> None:
    """Svuota la cache deleghe e rimuove i file delega nel pod middlebox."""
    run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote('pkill -USR1 -f middlebox 2>/dev/null || true')}",
        check=False,
    )
    time.sleep(0.1)
    run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote('rm -f /certs/dc.cred /certs/dckey.pem 2>/dev/null; true')}",
        check=False,
    )
    verify_cmd = (
        "if [ -f /certs/dc.cred ] || [ -f /certs/dckey.pem ]; then "
        "echo '[MB] Deleghe non eliminate'; exit 1; "
        "fi"
    )
    run_kubectl(
        f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
        f"sh -lc {quote(verify_cmd)}",
    )


def maybe_load_kind_image_remote() -> None:
    """Nessun preload immagine automatico: gestione solo Kubernetes.
    Se il pod fallisce con ErrImageNeverPull, assicurare che l'immagine sia disponibile nel cluster."""
    context = run_kubectl("config current-context", check=False).strip()
    if not context.startswith("kind-"):
        return
    print(
        f"[K8S] Context kind rilevato ({context}). "
        "Preload immagine disabilitato nello script: il pod userà l'immagine già presente nel cluster."
    )


def stop_middlebox_portforward_remote() -> None:
    """Ferma eventuale port-forward kubectl in background sul nodo middlebox."""
    cmd = (
        f"if [ -f {quote(REMOTE_MB_PORTFWD_PIDFILE)} ]; then "
        f"  pid=$(cat {quote(REMOTE_MB_PORTFWD_PIDFILE)}); "
        "  kill $pid >/dev/null 2>&1 || true; "
        "  sleep 0.5; "
        "  kill -9 $pid >/dev/null 2>&1 || true; "
        f"  rm -f {quote(REMOTE_MB_PORTFWD_PIDFILE)}; "
        "fi; "
        "pkill -f '[k]ubectl.*port-forward.*middlebox' >/dev/null 2>&1 || true"
    )
    run_ssh(INDIRIZZO_MIDDLEBOX, cmd, check=False)


def start_middlebox_portforward_remote() -> None:
    """Espone il pod middlebox su 0.0.0.0:8443 del nodo middlebox via kubectl port-forward.
    Serve quando il NodePort kind non è raggiungibile dal client esterno."""
    stop_middlebox_portforward_remote()

    kubectl_bin = discover_kubectl_path()
    kubeconfig = discover_kubeconfig_path()
    if kubeconfig:
        kubectl_pf = (
            f"{kubectl_bin} --kubeconfig {quote(kubeconfig)} -n {quote(NAMESPACE)} "
            f"port-forward --address 0.0.0.0 pod/{quote(MIDDLEBOX_POD)} {MIDDLEBOX_PORT}:{MIDDLEBOX_PORT}"
        )
    else:
        kubectl_pf = (
            f"{kubectl_bin} -n {quote(NAMESPACE)} "
            f"port-forward --address 0.0.0.0 pod/{quote(MIDDLEBOX_POD)} {MIDDLEBOX_PORT}:{MIDDLEBOX_PORT}"
        )

    start_cmd = (
        "set -e; "
        f"mkdir -p {quote(REMOTE_RUNTIME_DIR_MIDDLEBOX)}; "
        f"nohup {kubectl_pf} >> {quote(REMOTE_MB_PORTFWD_LOG)} 2>&1 < /dev/null & "
        f"echo $! > {quote(REMOTE_MB_PORTFWD_PIDFILE)}"
    )
    run_ssh(INDIRIZZO_MIDDLEBOX, start_cmd, check=True)

    deadline = time.time() + 20
    while time.time() < deadline:
        probe = run_ssh(
            INDIRIZZO_MIDDLEBOX,
            f"ss -ltn | grep -E ':{MIDDLEBOX_PORT}\\s' >/dev/null && echo ok || true",
            check=False,
        ).strip()
        if probe == "ok":
            return
        time.sleep(0.5)

    logs = run_ssh(INDIRIZZO_MIDDLEBOX, f"tail -n 80 {quote(REMOTE_MB_PORTFWD_LOG)} 2>/dev/null || true", check=False)
    raise RuntimeError(
        "Port-forward middlebox non avviato sulla macchina middlebox.\n"
        f"Dettagli log:\n{logs}"
    )


############################################
# SERVER REMOTO (via SSH)
############################################

def start_server_remote() -> None:
    start_idx = len(read_remote_file(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG).encode("utf-8"))
    stop_cmd = "pkill -f '[c]erts_server.py' >/dev/null 2>&1 || true"
    run_ssh(INDIRIZZO_SERVER, stop_cmd, check=False)

    remote_generate_tool_abs = f"/home/{SSH_USER}/{REMOTE_GENERATE_TOOL}"
    start_cmd = (
        f"set -e; "
        f"cd {quote(REMOTE_PROJECT_ROOT_SERVER)}; "
        "mkdir -p TreDispositivi/MisureKubernetes/Runtime; "
        f"GO_TOOL={quote(remote_generate_tool_abs)} "
        "nohup python3 -u PerformanceMeasuring/certs_server.py "
        ">> TreDispositivi/MisureKubernetes/Runtime/Server.log 2>&1 < /dev/null & "
        "echo $! > TreDispositivi/MisureKubernetes/server.pid"
    )
    run_ssh(INDIRIZZO_SERVER, start_cmd)

    marker = "[SERVER] Application TLS server running on :8000"
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        remote_content = read_remote_file(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG)
        if marker in remote_content[start_idx:]:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Marker server non trovato: {marker}")


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


############################################
# CLIENT (locale)
############################################

def client_request(target: str) -> str:
    url = f"https://{target}/function/init"

    cmd = [
        str(CLIENT_BINARY),
        "-ca", str(CLIENT_CA_HOST_PATH),
        "-servername", "server",
        "-H", f"Authorization : Bearer {TOKEN}",
    ]
    if TYPE.upper() == "POST":
        cmd.extend(["-data", "{}"])
    elif TYPE.upper() != "GET":
        raise ValueError("TYPE deve essere GET oppure POST")
    cmd.append(url)

    result = subprocess.run(
        cmd,
        cwd=str(MB_DIR),
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT,
    )
    output = (result.stdout or "") + (result.stderr or "")
    append_text(CLIENT_RUNTIME_LOG, output)

    has_call = "Calling:" in output
    has_ok = '"status": "ok"' in output
    has_error = (
        "Error during request" in output
        or "connection refused" in output
        or "panic:" in output
    )
    if not has_call or not has_ok or has_error:
        diagnostics = ""
        try:
            diagnostics = "\n" + collect_request_failure_diagnostics()
        except Exception as exc:
            diagnostics = f"\n[DIAG] impossibile raccogliere diagnostica aggiuntiva: {exc}"
        raise RuntimeError(f"Richiesta client fallita verso {url}\n{output}{diagnostics}")
    return output


def collect_request_failure_diagnostics() -> str:
    chunks: list[str] = []
    chunks.append("[DIAG] Server.log tail")
    chunks.append(run_ssh(INDIRIZZO_SERVER, f"tail -n 120 {quote(REMOTE_SERVER_RUNTIME_LOG)} 2>/dev/null || true", check=False))

    chunks.append("[DIAG] Pod middlebox /tmp/Middlebox.log tail")
    chunks.append(
        run_kubectl(
            f"-n {quote(NAMESPACE)} exec {quote(MIDDLEBOX_POD)} -- "
            f"sh -lc {quote(f'tail -n 120 {MB_LOG_PATH_IN_POD} 2>/dev/null || true')}",
            check=False,
        )
    )

    chunks.append("[DIAG] Server service/endpoints")
    chunks.append(run_kubectl(f"-n {quote(NAMESPACE)} get svc server -o wide", check=False))
    chunks.append(run_kubectl(f"-n {quote(NAMESPACE)} get endpoints server -o wide", check=False))
    return "\n".join(chunks)


def wait_local_tcp_reachable(host: str, port: int, timeout_sec: float = 20.0) -> None:
    """Attende che l'endpoint sia raggiungibile via TCP dal client locale.
    Evita timeout lunghi del binario client quando il path rete non e pronto."""
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.3)
    raise TimeoutError(
        f"Endpoint middlebox non raggiungibile dal client locale: {host}:{port}. "
        f"Ultimo errore socket: {last_error}"
    )


def local_has_ip(ip: str) -> bool:
    output = run_local_proc(["bash", "-lc", "ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1"], check=False)
    ips = {line.strip() for line in output.splitlines() if line.strip()}
    return ip in ips


def remote_has_ip(host: str, ip: str) -> bool:
    output = run_ssh(
        host,
        "ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1",
        check=False,
    )
    ips = {line.strip() for line in output.splitlines() if line.strip()}
    return ip in ips


def ensure_expected_network_topology() -> None:
    problems: list[str] = []
    if not local_has_ip(CLIENT_MIDDLEBOX_FACING_IP):
        problems.append(
            f"Client locale senza IP atteso verso middlebox: {CLIENT_MIDDLEBOX_FACING_IP}"
        )
    if not remote_has_ip(INDIRIZZO_MIDDLEBOX, MIDDLEBOX_CLIENT_FACING_IP):
        problems.append(
            f"Middlebox {INDIRIZZO_MIDDLEBOX} senza IP client-facing atteso: {MIDDLEBOX_CLIENT_FACING_IP}"
        )
    if not remote_has_ip(INDIRIZZO_MIDDLEBOX, MIDDLEBOX_SERVER_FACING_IP):
        problems.append(
            f"Middlebox {INDIRIZZO_MIDDLEBOX} senza IP server-facing atteso: {MIDDLEBOX_SERVER_FACING_IP}"
        )
    if not remote_has_ip(INDIRIZZO_SERVER, SERVER_MIDDLEBOX_FACING_IP):
        problems.append(
            f"Server {INDIRIZZO_SERVER} senza IP atteso verso middlebox: {SERVER_MIDDLEBOX_FACING_IP}"
        )

    if problems:
        raise RuntimeError(
            "Topologia di rete non conforme ai requisiti dichiarati:\n- "
            + "\n- ".join(problems)
        )


def cleanup_before_experiments_from_client() -> None:
    """Cleanup orchestrato dal client prima del primo esperimento.
    Riduce interferenze da run precedenti (pod/port-forward/server ancora attivi)."""
    print("[PREP] Cleanup iniziale orchestrato dal client...")

    stop_local_middlebox_tunnel()

    # Pulizia remota middlebox (kubectl port-forward e listener 8443 residui)
    stop_middlebox_portforward_remote()
    run_ssh(
        INDIRIZZO_MIDDLEBOX,
        "pkill -f '[k]ubectl.*port-forward.*8443' >/dev/null 2>&1 || true; "
        "pkill -f '[k]ubectl.*port-forward.*middlebox' >/dev/null 2>&1 || true",
        check=False,
    )

    # Cleanup pod/service precedente best-effort
    if kubernetes_api_reachable():
        delete_middlebox_pod()

    # Cleanup server remoto precedente
    stop_server_remote()


def collect_middlebox_connectivity_diagnostics() -> str:
    chunks: list[str] = []
    chunks.append("[DIAG] Middlebox listener 8443")
    chunks.append(run_ssh(INDIRIZZO_MIDDLEBOX, f"ss -ltnp | grep ':{MIDDLEBOX_PORT}' || true", check=False))

    chunks.append("[DIAG] Port-forward log tail")
    chunks.append(run_ssh(INDIRIZZO_MIDDLEBOX, f"tail -n 120 {quote(REMOTE_MB_PORTFWD_LOG)} 2>/dev/null || true", check=False))

    chunks.append("[DIAG] Pod middlebox status")
    chunks.append(run_kubectl(f"-n {quote(NAMESPACE)} get pod {quote(MIDDLEBOX_POD)} -o wide", check=False))

    chunks.append("[DIAG] Middlebox pod logs tail")
    chunks.append(run_kubectl(f"-n {quote(NAMESPACE)} logs {quote(MIDDLEBOX_POD)} --tail=80", check=False))

    chunks.append("[DIAG] Local tunnel log tail")
    if LOCAL_MB_TUNNEL_LOG.exists():
        chunks.append(read_text(LOCAL_MB_TUNNEL_LOG)[-4000:])
    else:
        chunks.append("(assente)")

    return "\n".join(chunks)


def _read_local_tunnel_pid() -> int | None:
    if not LOCAL_MB_TUNNEL_PIDFILE.exists():
        return None
    try:
        return int(LOCAL_MB_TUNNEL_PIDFILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def stop_local_middlebox_tunnel() -> None:
    global _LOCAL_TUNNEL_PROC

    pid = _read_local_tunnel_pid()
    if pid is not None:
        run_local_proc(["bash", "-lc", f"kill {pid} >/dev/null 2>&1 || true; sleep 0.2; kill -9 {pid} >/dev/null 2>&1 || true"], check=False)
        try:
            LOCAL_MB_TUNNEL_PIDFILE.unlink(missing_ok=True)
        except Exception:
            pass

    run_local_proc(
        [
            "bash",
            "-lc",
            "pkill -f '[s]sh.*127.0.0.1:18443:127.0.0.1:8443' >/dev/null 2>&1 || true",
        ],
        check=False,
    )
    _LOCAL_TUNNEL_PROC = None


def start_local_middlebox_tunnel() -> None:
    global _LOCAL_TUNNEL_PROC

    stop_local_middlebox_tunnel()
    LOCAL_MB_TUNNEL_LOG.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
    ]
    if SSH_KEY_PATH.exists():
        cmd += ["-i", str(SSH_KEY_PATH)]
    cmd += [
        "-N",
        "-L",
        f"{LOCAL_MB_TUNNEL_HOST}:{LOCAL_MB_TUNNEL_PORT}:{LOCAL_MB_TUNNEL_REMOTE_HOST}:{MIDDLEBOX_PORT}",
        f"{SSH_USER}@{INDIRIZZO_MIDDLEBOX}",
    ]

    with LOCAL_MB_TUNNEL_LOG.open("ab") as logf:
        _LOCAL_TUNNEL_PROC = subprocess.Popen(cmd, stdout=logf, stderr=logf)

    LOCAL_MB_TUNNEL_PIDFILE.write_text(str(_LOCAL_TUNNEL_PROC.pid), encoding="utf-8")

    # Verifica rapida che il processo non sia terminato e che il forward sia raggiungibile.
    time.sleep(0.4)
    if _LOCAL_TUNNEL_PROC.poll() is not None:
        tail = read_text(LOCAL_MB_TUNNEL_LOG)[-2000:] if LOCAL_MB_TUNNEL_LOG.exists() else ""
        raise RuntimeError(
            "Tunnel SSH locale client->middlebox terminato subito dopo l'avvio.\n"
            f"Comando: {' '.join(cmd)}\n"
            f"Log:\n{tail}"
        )
    wait_local_tcp_reachable(LOCAL_MB_TUNNEL_HOST, LOCAL_MB_TUNNEL_PORT, timeout_sec=8.0)


def select_reachable_middlebox_host() -> str:
    """Seleziona endpoint middlebox raggiungibile dal client locale.
    Priorita: IP client-facing 172.16.1.1, fallback su IP pubblico middlebox."""
    attempts: list[tuple[str, float]] = [
        (MIDDLEBOX_CLIENT_FACING_IP, 18.0),
        (MIDDLEBOX_PUBLIC_IP, 8.0),
    ]
    failures: list[str] = []
    for host, timeout_sec in attempts:
        try:
            wait_local_tcp_reachable(host, MIDDLEBOX_PORT, timeout_sec=timeout_sec)
            if host != MIDDLEBOX_CLIENT_FACING_IP:
                print(
                    f"[NET] Warning: endpoint {MIDDLEBOX_CLIENT_FACING_IP}:{MIDDLEBOX_PORT} non raggiungibile; "
                    f"uso fallback {host}:{MIDDLEBOX_PORT}."
                )
            return host
        except Exception as exc:
            failures.append(f"{host}:{MIDDLEBOX_PORT} -> {exc}")

    # Fallback definitivo: tunnel SSH locale (client -> middlebox:8443).
    try:
        start_local_middlebox_tunnel()
        print(
            f"[NET] Uso tunnel SSH locale {LOCAL_MB_TUNNEL_HOST}:{LOCAL_MB_TUNNEL_PORT} "
            f"-> {INDIRIZZO_MIDDLEBOX}:{MIDDLEBOX_PORT}"
        )
        return LOCAL_MB_TUNNEL_HOST
    except Exception as exc:
        failures.append(
            f"{LOCAL_MB_TUNNEL_HOST}:{LOCAL_MB_TUNNEL_PORT} (tunnel SSH) -> {exc}"
        )

    diagnostics = collect_middlebox_connectivity_diagnostics()
    raise TimeoutError(
        "Nessun endpoint middlebox raggiungibile dal client locale.\n"
        + "Tentativi:\n- "
        + "\n- ".join(failures)
        + "\n"
        + diagnostics
    )


############################################
# MARKER E SINCRONIZZAZIONE LOG
############################################

def write_experiment_markers(exp: int) -> None:
    marker = f"\n===== ESPERIMENTO {exp} =====\n"
    append_text(CLIENT_RUNTIME_LOG, marker)
    append_text(MIDDLEBOX_RUNTIME_LOG, marker)
    append_remote(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG, marker)
    append_text(CONTAINER_TIMES_LOG, marker)


def write_operation_marker(exp: int, op: int) -> dict[str, int]:
    marker = f"--- OPERAZIONE {op} (esperimento {exp}) ---\n"
    append_text(CLIENT_RUNTIME_LOG, marker)
    append_text(MIDDLEBOX_RUNTIME_LOG, marker)
    append_remote(INDIRIZZO_SERVER, REMOTE_SERVER_RUNTIME_LOG, marker)
    sync_server_log()
    return {
        "Client": file_size(CLIENT_RUNTIME_LOG),
        "Middlebox": file_size(MIDDLEBOX_RUNTIME_LOG),
        "Server": file_size(SERVER_RUNTIME_LOG),
    }


def parse_ordered_times(text: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for line in text.splitlines():
        for match in TIMESTAMP_PATTERN.finditer(line):
            out.append((int(match.group(1)), int(match.group(2))))
    return out


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
        time.sleep(0.3)

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


def wait_logs_after_operation(op: int, start_offsets: dict[str, int]) -> None:
    wait_for_expected_timestamps(op, start_offsets)
    wait_until_log_settles([CLIENT_RUNTIME_LOG, MIDDLEBOX_RUNTIME_LOG, SERVER_RUNTIME_LOG])
    time.sleep(0.2)


############################################
# ANALISI DA LOG AGGREGATI
############################################

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
    reconstructed: dict[int, dict[int, dict[int, int]]] = {
        exp: {1: {}, 2: {}, 3: {}} for exp in all_exp
    }

    client_allowed = set().union(*EXPECTED_TIMESTAMPS["Client"].values())
    middlebox_allowed = set().union(*EXPECTED_TIMESTAMPS["Middlebox"].values())
    server_allowed = set().union(*EXPECTED_TIMESTAMPS["Server"].values())

    for exp in all_exp:
        client_ops = latest_times_per_operation(client_sections.get(exp, ""), client_allowed)
        middlebox_ops = latest_times_per_operation(middlebox_sections.get(exp, ""), middlebox_allowed)
        server_ops = latest_times_per_operation(server_sections.get(exp, ""), server_allowed)

        for op in range(1, 4):
            merged: dict[int, int] = {}
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
        op: {"times": defaultdict(list), "diffs": defaultdict(list)}
        for op in range(1, 4)
    }
    graph_points: dict[int, dict[str, list[tuple[int, int]]]] = {
        op: {"t1": [], "t10": [], "t10 - t1": []} for op in range(1, 4)
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

        diff_order = [
            "t2 - t1", "t28 - t27", "t3 - t2", "t4 - t3", "t38 - t37", "t39 - t38", "t40 - t39",
            "t26 - t25", "t5 - t4", "t6 - t5", "t7 - t6", "t8 - t7", "t11 - t8", "t12 - t11",
            "t13 - t12", "t14 - t13", "t15 - t14", "t16 - t15", "t17 - t16", "t18 - t17",
            "t19 - t18", "t9 - t19", "t9 - t8", "t10 - t9", "t10 - t40", "t10 - t1",
            "t21 - t20", "t23 - t22",
        ]
        for op in range(1, 4):
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
            for op in range(1, 4):
                graphs_file.write(f"SERIE {metric} operazione_N_{op}:\n")
                for exp, value in graph_points[op][metric]:
                    graphs_file.write(f"{series_label(exp, op)} = {value}\n")
                graphs_file.write("--------------------\n")


############################################
# GRAFICI (basati su GraficiKubernetes.py)
############################################

def generate_graphs_like_grafici_kubernetes() -> None:
    gk = importlib.import_module("GraficiKubernetes")

    gk.BASE_DIR = ANALYSIS_DIR
    gk.OUTPUT_TXT = ANALYSIS_DIR / "Grafici.txt"
    gk.DIAGNOSTIC_TXT = ANALYSIS_DIR / "DiagnosticaRuntime.txt"
    gk.OUTPUT_PDF_DIR = ANALYSIS_DIR

    series = gk.parse_metric_series(RUNTIME_DIR)
    gk.write_grafici_txt(series, RUNTIME_DIR)
    gk.write_runtime_diagnostics(series, RUNTIME_DIR)
    gk.make_plots(series, show=False, save_pdf=True)

    explosion_by_op = gk.build_timestamp_avg_for_explosion_by_operation(series)
    for op in gk.OPS:
        gk.render_t1_t10_explosion_chart(
            timestamp_avg_ns=explosion_by_op.get(op, {}),
            output_path=gk.OUTPUT_PDF_DIR / f"T1_T10_Explosion_Operazione_{op}.png",
            title=f"Esplosione temporale t1->t10 - Operazione N_{op}",
            alpha=gk.EXPLOSION_ALPHA,
        )


############################################
# ESPERIMENTI
############################################

def stop_experiment_services() -> None:
    stop_local_middlebox_tunnel()
    stop_middlebox_portforward_remote()
    stop_service_in_pod("middlebox")
    stop_server_remote()
    time.sleep(1)


def run_single_experiment(exp: int) -> None:
    middlebox_addr = ""

    print(f"\n===== ESPERIMENTO {exp} =====\n")
    write_experiment_markers(exp)
    delete_middlebox_pod()

    try:
        create_middlebox_pod(exp)
        start_server_remote()
        start_middlebox_in_pod()
        start_middlebox_portforward_remote()
        selected_host = select_reachable_middlebox_host()
        selected_port = LOCAL_MB_TUNNEL_PORT if selected_host == LOCAL_MB_TUNNEL_HOST else MIDDLEBOX_PORT
        middlebox_addr = f"{selected_host}:{selected_port}"
        print(f"[NET] Target middlebox selezionato per il client: {middlebox_addr}")

        print("OPERAZIONE 1")
        offsets = write_operation_marker(exp, 1)
        client_request(middlebox_addr)
        wait_logs_after_operation(1, offsets)

        print("OPERAZIONE 2")
        offsets = write_operation_marker(exp, 2)
        reset_middlebox_delegation()
        client_request(middlebox_addr)
        wait_logs_after_operation(2, offsets)

        print("OPERAZIONE 3")
        offsets = write_operation_marker(exp, 3)
        client_request(middlebox_addr)
        wait_logs_after_operation(3, offsets)

    finally:
        stop_experiment_services()
        delete_middlebox_pod()


def run_experiments() -> None:
    global _MIDDLEBOX_NODEPORT_CACHE

    reset_output_directories()
    ensure_directories()
    ensure_kubernetes_cluster_ready()
    ensure_external_certificates()
    ensure_server_certificates_remote()
    ensure_server_script_remote()
    ensure_server_generate_tool_remote()
    ensure_client_binary()
    ensure_remote_server_layout()
    reset_remote_server_log()

    # Eseguito dal client PRIMA degli esperimenti: cleanup stato residuo + validazione topologia.
    cleanup_before_experiments_from_client()
    ensure_expected_network_topology()

    _MIDDLEBOX_NODEPORT_CACHE = None
    global _KUBECTL_PATH_CACHE
    _KUBECTL_PATH_CACHE = None
    global _KUBECONFIG_PATH_CACHE
    _KUBECONFIG_PATH_CACHE = None

    # In setup kind il client esterno usa port-forward host:8443, non NodePort.
    # Manteniamo il service middlebox opzionale per compatibilità, ma non blocchiamo il run se fallisce.
    try:
        ensure_middlebox_nodeport_service()
    except Exception as exc:
        print(f"[K8S] Warning: impossibile garantire middlebox NodePort service: {exc}")
    ensure_server_cluster_service()
    maybe_load_kind_image_remote()

    for exp in range(1, N + 1):
        run_single_experiment(exp)

    # Sincronizzazione finale completa dei log remoti
    sync_server_log()
    sync_middlebox_log_increment()


############################################
# CLEANUP E VERIFICA
############################################

def emergency_cleanup() -> None:
    try:
        stop_local_middlebox_tunnel()
    except Exception:
        pass
    try:
        stop_middlebox_portforward_remote()
    except Exception:
        pass
    if not kubernetes_api_reachable():
        print("[CLEANUP] Cluster Kubernetes non raggiungibile: skip cleanup pod/service sul middlebox")
    else:
        try:
            stop_service_in_pod("middlebox")
        except Exception:
            pass
        try:
            delete_middlebox_pod()
        except Exception:
            pass
        try:
            run_kubectl(
                f"-n {quote(NAMESPACE)} delete svc {quote(MIDDLEBOX_SVC)} "
                "--ignore-not-found=true --wait=false",
                check=False,
            )
        except Exception:
            pass
    try:
        stop_server_remote()
    except Exception:
        pass


def verify_cleanup() -> bool:
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
            print(f"Porta {port} libera su {host}")
    return all_clear


############################################
# MAIN
############################################

def main() -> None:
    try:
        run_experiments()
        analyze_logs()
        generate_graphs_like_grafici_kubernetes()
        print("\nEsperimenti Kubernetes (tre dispositivi) completati")
        print(f"Runtime: {RUNTIME_DIR}")
        print(f"Analisi e grafici: {ANALYSIS_DIR}")
    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            emergency_cleanup()
            verify_cleanup()
        except Exception as exc:
            print(f"Errore durante cleanup: {exc}")


if __name__ == "__main__":
    main()
