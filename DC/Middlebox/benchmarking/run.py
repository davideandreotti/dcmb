#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - depends on local machine setup
    psutil = None

try:
    import yaml
except ImportError as exc:  # pragma: no cover - depends on local machine setup
    raise SystemExit("Missing dependency: PyYAML. Install with: pip install pyyaml") from exc


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GO = PROJECT_DIR.parent / "go" / "bin" / "go"


def now_ns() -> int:
    return time.time_ns()


def timestamp_name() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def sanitize(value: Any) -> str:
    text = str(value).replace("/", "_").replace(" ", "_")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)


def ensure_local_host(host_cfg: dict[str, Any], role: str) -> None:
    host = str(host_cfg.get("host", "localhost"))
    if host not in ("", "localhost", "127.0.0.1"):
        raise NotImplementedError(
            f"First controller pass is local-only; role {role!r} is configured for host {host!r}"
        )


def deep_copy_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value))


@dataclass
class ManagedProcess:
    role: str
    cmd: list[str]
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    env: dict[str, str] = field(default_factory=dict)
    ready_patterns: list[str] = field(default_factory=list)

    proc: subprocess.Popen[str] | None = None
    started_ns: int | None = None
    ready_ns: int | None = None
    ready_line: str = ""
    _ready_event: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)

    def start(self) -> None:
        self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        full_env = os.environ.copy()
        full_env.update(self.env)
        self.started_ns = now_ns()
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self._threads = [
            threading.Thread(
                target=self._copy_stream,
                args=(self.proc.stdout, self.stdout_path),
                daemon=True,
            ),
            threading.Thread(
                target=self._copy_stream,
                args=(self.proc.stderr, self.stderr_path),
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def _copy_stream(self, stream: Any, path: Path) -> None:
        with path.open("w", encoding="utf-8", buffering=1) as handle:
            for line in stream:
                handle.write(line)
                if self.ready_patterns and not self._ready_event.is_set():
                    if any(pattern in line for pattern in self.ready_patterns):
                        self.ready_ns = now_ns()
                        self.ready_line = line.strip()
                        self._ready_event.set()

    def wait_ready(self, timeout_s: float) -> None:
        if not self.ready_patterns:
            return
        if self._ready_event.wait(timeout_s):
            return
        if self.proc and self.proc.poll() is not None:
            raise RuntimeError(f"{self.role} exited before readiness; returncode={self.proc.returncode}")
        raise TimeoutError(f"{self.role} did not print readiness within {timeout_s}s")

    def poll(self) -> int | None:
        if self.proc is None:
            return None
        return self.proc.poll()

    def wait(self, timeout_s: float | None = None) -> int | None:
        if self.proc is None:
            return None
        try:
            return self.proc.wait(timeout=timeout_s)
        finally:
            self.join_readers()

    def stop(self, grace_s: float = 5.0) -> int | None:
        if self.proc is None:
            return None
        try:
            if self.proc.poll() is None:
                self._signal_group(signal.SIGINT)
                try:
                    return self.proc.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    self._signal_group(signal.SIGTERM)
                try:
                    return self.proc.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    self._signal_group(signal.SIGKILL)
                    return self.proc.wait(timeout=grace_s)
            return self.proc.returncode
        finally:
            self.join_readers()

    def join_readers(self) -> None:
        for thread in self._threads:
            thread.join(timeout=1)

    def _signal_group(self, sig: signal.Signals) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def metadata(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "cmd": self.cmd,
            "cwd": str(self.cwd),
            "pid": self.proc.pid if self.proc else None,
            "returncode": self.proc.returncode if self.proc else None,
            "started_ns": self.started_ns,
            "ready_ns": self.ready_ns,
            "startup_ns": (self.ready_ns - self.started_ns) if self.ready_ns and self.started_ns else None,
            "ready_line": self.ready_line,
            "stdout": str(self.stdout_path),
            "stderr": str(self.stderr_path),
        }


class ProcessCPUMonitor:
    def __init__(self, rows: list[tuple[str, ManagedProcess]], csv_path: Path, interval_s: float) -> None:
        self.rows = rows
        self.csv_path = csv_path
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if psutil is None:
            print("[WARN] psutil is not installed; process CPU monitor disabled")
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _run(self) -> None:
        assert psutil is not None
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "ts_ns",
                    "role",
                    "pid",
                    "returncode",
                    "user_time_s",
                    "system_time_s",
                    "rss_bytes",
                ],
            )
            writer.writeheader()
            while not self.stop_event.is_set():
                ts = now_ns()
                for role, managed in self.rows:
                    if managed.proc is None:
                        continue
                    try:
                        proc = psutil.Process(managed.proc.pid)
                        cpu = proc.cpu_times()
                        mem = proc.memory_info()
                        writer.writerow(
                            {
                                "ts_ns": ts,
                                "role": role,
                                "pid": proc.pid,
                                "returncode": managed.poll(),
                                "user_time_s": cpu.user,
                                "system_time_s": cpu.system,
                                "rss_bytes": mem.rss,
                            }
                        )
                    except psutil.Error:
                        continue
                handle.flush()
                time.sleep(self.interval_s)


class ContainerStatsMonitor:
    def __init__(
        self,
        runtime: str,
        names: list[str],
        prefixes: list[str],
        csv_path: Path,
        total_csv_path: Path,
        interval_s: float,
        scope: str = "matching",
    ) -> None:
        self.runtime = runtime
        self.names = names
        self.prefixes = prefixes
        self.csv_path = csv_path
        self.total_csv_path = total_csv_path
        self.interval_s = interval_s
        self.scope = scope
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not shutil.which(self.runtime):
            print(f"[WARN] {self.runtime} not found; container stats disabled")
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _include(self, name: str) -> bool:
        if self.scope == "all":
            return True
        return name in self.names or any(name.startswith(prefix) for prefix in self.prefixes)

    def _matching_container_names(self) -> list[str]:
        result = subprocess.run(
            [self.runtime, "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if self._include(line.strip())]

    def _parse_percent(self, raw: str) -> float:
        try:
            return float(str(raw).strip().rstrip("%"))
        except ValueError:
            return 0.0

    def _parse_size_bytes(self, raw: str) -> float:
        value = str(raw).strip().split("/", 1)[0].strip()
        if not value:
            return 0.0
        units = {
            "B": 1,
            "kB": 1000,
            "KB": 1000,
            "KiB": 1024,
            "MB": 1000**2,
            "MiB": 1024**2,
            "GB": 1000**3,
            "GiB": 1024**3,
        }
        for unit, scale in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
            if value.endswith(unit):
                try:
                    return float(value[: -len(unit)].strip()) * scale
                except ValueError:
                    return 0.0
        try:
            return float(value)
        except ValueError:
            return 0.0

    def _parse_int(self, raw: str) -> int:
        try:
            return int(str(raw).strip())
        except ValueError:
            return 0

    def _run(self) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle, self.total_csv_path.open(
            "w", newline="", encoding="utf-8"
        ) as total_handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "ts_ns",
                    "runtime",
                    "name",
                    "id",
                    "cpu_perc",
                    "mem_usage",
                    "mem_perc",
                    "net_io",
                    "block_io",
                    "pids",
                ],
            )
            writer.writeheader()
            total_writer = csv.DictWriter(
                total_handle,
                fieldnames=[
                    "ts_ns",
                    "runtime",
                    "scope",
                    "container_count",
                    "cpu_perc_sum",
                    "mem_usage_bytes_sum",
                    "pids_sum",
                ],
            )
            total_writer.writeheader()
            last_timeout_warning = 0.0
            while not self.stop_event.is_set():
                ts = now_ns()
                try:
                    names = self._matching_container_names()
                    if not names:
                        time.sleep(self.interval_s)
                        continue
                    cmd = [self.runtime, "stats", "--no-stream", "--format", "{{json .}}", *names]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                except subprocess.TimeoutExpired:
                    if time.time() - last_timeout_warning > 5:
                        print(f"[WARN] {self.runtime} stats timed out; skipping container resource sample")
                        last_timeout_warning = time.time()
                    time.sleep(self.interval_s)
                    continue

                if result.returncode == 0:
                    container_count = 0
                    cpu_sum = 0.0
                    mem_sum = 0.0
                    pids_sum = 0
                    for line in result.stdout.splitlines():
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        name = item.get("Name", "")
                        if not self._include(name):
                            continue
                        container_count += 1
                        cpu_sum += self._parse_percent(item.get("CPUPerc", ""))
                        mem_sum += self._parse_size_bytes(item.get("MemUsage", ""))
                        pids_sum += self._parse_int(item.get("PIDs", ""))
                        writer.writerow(
                            {
                                "ts_ns": ts,
                                "runtime": self.runtime,
                                "name": name,
                                "id": item.get("Container", ""),
                                "cpu_perc": item.get("CPUPerc", ""),
                                "mem_usage": item.get("MemUsage", ""),
                                "mem_perc": item.get("MemPerc", ""),
                                "net_io": item.get("NetIO", ""),
                                "block_io": item.get("BlockIO", ""),
                                "pids": item.get("PIDs", ""),
                            }
                        )
                    total_writer.writerow(
                        {
                            "ts_ns": ts,
                            "runtime": self.runtime,
                            "scope": self.scope,
                            "container_count": container_count,
                            "cpu_perc_sum": f"{cpu_sum:.6f}",
                            "mem_usage_bytes_sum": int(mem_sum),
                            "pids_sum": pids_sum,
                        }
                    )
                    handle.flush()
                    total_handle.flush()
                time.sleep(self.interval_s)


class Deployment:
    def __init__(self, cfg: dict[str, Any], controller: "Controller", run: "RunContext") -> None:
        self.cfg = cfg
        self.controller = controller
        self.run = run
        self.proc: ManagedProcess | None = None

    @property
    def name(self) -> str:
        return str(self.cfg["name"])

    @property
    def kind(self) -> str:
        return str(self.cfg["kind"])

    def cleanup_before(self) -> None:
        return

    def start(self) -> ManagedProcess:
        raise NotImplementedError

    def stop(self) -> None:
        if self.proc:
            self.proc.stop(grace_s=8)

    def client_url(self) -> str:
        raise NotImplementedError

    def container_monitor(self) -> ContainerStatsMonitor | None:
        return None


class BaremetalDeployment(Deployment):
    def start(self) -> ManagedProcess:
        command = list(self.cfg.get("command", ["./middlebox", "-log_level", "debug", "-minimal_logs=false"]))
        command.extend(
            [
                "-trace",
                str(self.run.traces_dir / "middlebox.bin"),
                "-trace-buffer-events",
                str(self.controller.trace_buffer_events),
                f"-trace-drop-on-full={str(self.controller.trace_drop_on_full).lower()}",
            ]
        )
        env = {
            "OPERATOR_TARGET": self.controller.server_target_url,
            "OPERATOR_CERT_URL": self.controller.server_cert_url,
        }
        self.proc = self.controller.spawn(
            role="middlebox",
            cmd=command,
            cwd=self.controller.middlebox_dir,
            env=env,
            ready_patterns=["[OPERATOR_READY]"],
        )
        return self.proc

    def client_url(self) -> str:
        host = self.controller.hosts["middlebox"].get("ip", "127.0.0.1")
        return f"https://{host}:8443{self.controller.request_path}"


class DockerGatewayDeployment(Deployment):
    def cleanup_before(self) -> None:
        runtime = str(self.cfg.get("runtime", "docker"))
        container_name = str(self.cfg.get("container_name", "dcmb_gateway"))
        worker_prefix = str(self.cfg.get("worker_name_prefix", "dcmb-worker"))
        run_quiet([runtime, "rm", "-f", container_name])
        ids = list_container_ids(runtime, worker_prefix)
        if ids:
            run_quiet([runtime, "rm", "-f", *ids])

    def start(self) -> ManagedProcess:
        runtime = str(self.cfg.get("runtime", "docker"))
        container_name = str(self.cfg.get("container_name", "dcmb_gateway"))
        network = str(self.cfg.get("network", "dcmb-middlebox-net"))
        image = str(self.cfg.get("image", "dcmb_gateway:docker"))
        min_ready = str(self.cfg.get("min_ready", 1))
        scale_up_by = str(self.cfg.get("scale_up_by", min_ready))
        worker_trace_enabled = str(bool(self.cfg.get("worker_trace_enabled", False))).lower()
        traces_host = str(self.run.traces_dir.resolve())

        cmd = [
            runtime,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            network,
            "-p",
            "9443:9443",
            "-p",
            "8088:8088",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{traces_host}:/trace",
            "-e",
            "GATEWAY_BACKEND_MODE=docker",
            "-e",
            f"OPERATOR_TARGET={self.controller.server_target_url}",
            "-e",
            f"OPERATOR_CERT_URL={self.controller.server_cert_url}",
            "-e",
            f"DOCKER_MIN_READY_OPERATORS={min_ready}",
            "-e",
            f"DOCKER_SCALE_UP_BY={scale_up_by}",
            "-e",
            f"DOCKER_WORKER_TRACE_ENABLED={worker_trace_enabled}",
            "-e",
            f"DOCKER_WORKER_TRACE_HOST_DIR={traces_host}",
            "-e",
            "DOCKER_WORKER_TRACE_CONTAINER_DIR=/trace",
            "-e",
            f"DOCKER_API_TIMEOUT_MS={self.cfg.get('docker_api_timeout_ms', 5000)}",
            image,
            "-trace",
            "/trace/gateway.bin",
            "-trace-buffer-events",
            str(self.controller.trace_buffer_events),
            f"-trace-drop-on-full={str(self.controller.trace_drop_on_full).lower()}",
        ]

        self.proc = self.controller.spawn(
            role="gateway",
            cmd=cmd,
            cwd=self.controller.middlebox_dir,
            env={},
            ready_patterns=["[GATEWAY_READY]"],
        )
        return self.proc

    def stop(self) -> None:
        runtime = str(self.cfg.get("runtime", "docker"))
        container_name = str(self.cfg.get("container_name", "dcmb_gateway"))
        run_quiet([runtime, "stop", "-t", "15", container_name])
        if self.proc:
            self.proc.wait(timeout_s=20)
        worker_prefix = str(self.cfg.get("worker_name_prefix", "dcmb-worker"))
        ids = list_container_ids(runtime, worker_prefix)
        if ids:
            run_quiet([runtime, "rm", "-f", *ids])

    def client_url(self) -> str:
        host = self.controller.hosts["middlebox"].get("ip", "127.0.0.1")
        return f"https://{host}:9443{self.controller.request_path}"

    def container_monitor(self) -> ContainerStatsMonitor | None:
        runtime = str(self.cfg.get("runtime", "docker"))
        container_name = str(self.cfg.get("container_name", "dcmb_gateway"))
        worker_prefix = str(self.cfg.get("worker_name_prefix", "dcmb-worker"))
        return ContainerStatsMonitor(
            runtime=runtime,
            names=[container_name],
            prefixes=[worker_prefix],
            csv_path=self.run.cpu_dir / "containers.csv",
            total_csv_path=self.run.cpu_dir / "containers_total.csv",
            interval_s=self.controller.cpu_interval_s,
            scope=str(self.cfg.get("container_stats_scope", "matching")),
        )


@dataclass
class RunContext:
    name: str
    directory: Path

    @property
    def stdout_dir(self) -> Path:
        return self.directory / "stdout"

    @property
    def stderr_dir(self) -> Path:
        return self.directory / "stderr"

    @property
    def traces_dir(self) -> Path:
        return self.directory / "traces"

    @property
    def csv_dir(self) -> Path:
        return self.directory / "csv"

    @property
    def cpu_dir(self) -> Path:
        return self.directory / "cpu"

    def create(self) -> None:
        for path in [self.stdout_dir, self.stderr_dir, self.traces_dir, self.csv_dir, self.cpu_dir]:
            path.mkdir(parents=True, exist_ok=True)


def run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not cmd or not shutil.which(cmd[0]):
        return None
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)


def list_container_ids(runtime: str, name_prefix: str) -> list[str]:
    if not shutil.which(runtime):
        return []
    result = subprocess.run(
        [runtime, "ps", "-aq", "--filter", f"name={name_prefix}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class Controller:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.campaign = self.config.get("campaign", {})
        self.hosts = self.config.get("hosts", {})
        for role in ["client", "middlebox", "server"]:
            ensure_local_host(self.hosts.get(role, {}), role)

        paths = self.config.get("paths", {})
        self.middlebox_dir = Path(paths.get("middlebox_dir", PROJECT_DIR)).expanduser()
        self.output_root = Path(self.campaign.get("output_root", "experiments")).expanduser()
        if not self.output_root.is_absolute():
            self.output_root = self.middlebox_dir / self.output_root

        server_cfg = self.config.get("server", {})
        self.server_target_url = str(server_cfg.get("target_url", "https://127.0.0.1:8000"))
        self.server_cert_url = str(server_cfg.get("cert_url", "http://127.0.0.1:5000"))
        self.request_path = str(server_cfg.get("request_path", "/function/init"))
        self.server_cfg = server_cfg
        self.client_cfg = self.config.get("client", {})

        self.runs = int(self.campaign.get("runs", 1))
        self.duration_s = int(self.campaign.get("duration_s", 10))
        self.warmup_s = float(self.campaign.get("warmup_s", 0))
        self.cooldown_s = float(self.campaign.get("cooldown_s", 0))
        self.readiness_timeout_s = float(self.campaign.get("readiness_timeout_s", 30))
        self.cpu_interval_s = float(self.campaign.get("cpu_interval_s", 0.2))
        self.trace_buffer_events = int(self.campaign.get("trace_buffer_events", 100000))
        self.trace_drop_on_full = bool(self.campaign.get("trace_drop_on_full", True))

        campaign_name = sanitize(self.campaign.get("name", "campaign"))
        self.campaign_dir = self.output_root / f"{timestamp_name()}_{campaign_name}"
        self.summary_path = self.campaign_dir / "summary.csv"

    def run(self) -> None:
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.config_path, self.campaign_dir / "campaign.yml")
        print(f"[CTRL] campaign directory: {self.campaign_dir}")

        with self.summary_path.open("w", newline="", encoding="utf-8") as summary:
            writer = csv.DictWriter(
                summary,
                fieldnames=[
                    "run_name",
                    "deployment",
                    "mode",
                    "clients",
                    "rate",
                    "iteration",
                    "status",
                    "client_returncode",
                    "run_dir",
                ],
            )
            writer.writeheader()

            for deployment_cfg, mode, clients, rate, iteration in self.iter_matrix():
                run_name = self.run_name(deployment_cfg, mode, clients, rate, iteration)
                run_ctx = RunContext(run_name, self.campaign_dir / run_name)
                status = "ok"
                client_returncode = None
                try:
                    client_returncode = self.run_one(run_ctx, deployment_cfg, mode, clients, rate, iteration)
                except Exception as exc:
                    status = "failed"
                    print(f"[CTRL] run {run_name} failed: {exc}", file=sys.stderr)
                writer.writerow(
                    {
                        "run_name": run_name,
                        "deployment": deployment_cfg.get("name"),
                        "mode": mode,
                        "clients": clients,
                        "rate": rate,
                        "iteration": iteration,
                        "status": status,
                        "client_returncode": client_returncode,
                        "run_dir": str(run_ctx.directory),
                    }
                )
                summary.flush()
                if self.cooldown_s > 0:
                    time.sleep(self.cooldown_s)

    def iter_matrix(self) -> Any:
        matrix = self.config.get("client_matrix", {})
        deployments = self.config.get("deployments", [])
        modes = matrix.get("modes", ["fresh"])
        clients_values = matrix.get("clients", [1])
        rates = matrix.get("rates", [10])
        for deployment_cfg, mode, clients, rate in itertools.product(deployments, modes, clients_values, rates):
            if str(mode) == "fresh" and float(rate) <= 0:
                continue
            for iteration in range(1, self.runs + 1):
                yield deployment_cfg, str(mode), int(clients), float(rate), iteration

    def run_name(self, deployment_cfg: dict[str, Any], mode: str, clients: int, rate: float, iteration: int) -> str:
        rate_text = "closed" if rate <= 0 else f"rate{rate:g}"
        return sanitize(f"{deployment_cfg['name']}_clients{clients}_{mode}_{rate_text}_run{iteration}")

    def run_one(
        self,
        run_ctx: RunContext,
        deployment_cfg: dict[str, Any],
        mode: str,
        clients: int,
        rate: float,
        iteration: int,
    ) -> int | None:
        print(f"[CTRL] starting {run_ctx.name}")
        run_ctx.create()
        deployment = self.make_deployment(deployment_cfg, run_ctx)
        deployment.cleanup_before()

        metadata: dict[str, Any] = {
            "run_name": run_ctx.name,
            "parameters": {
                "deployment": deployment_cfg,
                "mode": mode,
                "clients": clients,
                "rate": rate,
                "iteration": iteration,
                "duration_s": self.duration_s,
            },
            "config": deep_copy_jsonable(self.config),
            "started_ns": now_ns(),
            "processes": {},
        }
        self.write_metadata(run_ctx, metadata)

        managed: list[tuple[str, ManagedProcess]] = []
        cpu_monitor: ProcessCPUMonitor | None = None
        container_monitor: ContainerStatsMonitor | None = None
        client_proc: ManagedProcess | None = None

        try:
            certserver = self.start_certserver(run_ctx)
            managed.append(("certserver", certserver))
            certserver.wait_ready(self.readiness_timeout_s)

            appserver = self.start_appserver(run_ctx)
            managed.append(("server", appserver))
            appserver.wait_ready(self.readiness_timeout_s)

            deployment_proc = deployment.start()
            managed.append((deployment.kind, deployment_proc))
            deployment_proc.wait_ready(self.readiness_timeout_s)

            if self.warmup_s > 0:
                time.sleep(self.warmup_s)

            cpu_monitor = ProcessCPUMonitor(managed, run_ctx.cpu_dir / "processes.csv", self.cpu_interval_s)
            cpu_monitor.start()
            container_monitor = deployment.container_monitor()
            if container_monitor:
                container_monitor.start()

            client_proc = self.start_client(run_ctx, mode, clients, rate, deployment.client_url())
            managed.append(("client", client_proc))
            client_proc.wait(timeout_s=self.duration_s + 30)
            returncode = client_proc.poll()
            print(f"[CTRL] client finished returncode={returncode}")
            return returncode
        finally:
            if client_proc and client_proc.poll() is None:
                client_proc.stop(grace_s=5)
            if cpu_monitor:
                cpu_monitor.stop()
            if container_monitor:
                container_monitor.stop()
            try:
                deployment.stop()
            except Exception as exc:
                print(f"[CTRL] deployment stop failed: {exc}", file=sys.stderr)
            for _, proc in reversed(managed):
                if proc is not client_proc and proc is not deployment.proc:
                    proc.stop(grace_s=5)
            metadata["finished_ns"] = now_ns()
            metadata["processes"] = {role: proc.metadata() for role, proc in managed}
            self.write_metadata(run_ctx, metadata)
            self.convert_traces(run_ctx)

    def make_deployment(self, cfg: dict[str, Any], run_ctx: RunContext) -> Deployment:
        kind = str(cfg.get("kind"))
        if kind == "baremetal":
            return BaremetalDeployment(cfg, self, run_ctx)
        if kind == "docker_gateway":
            return DockerGatewayDeployment(cfg, self, run_ctx)
        raise NotImplementedError(f"deployment kind {kind!r} is not in the first implementation scope")

    def spawn(
        self,
        role: str,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        ready_patterns: list[str],
    ) -> ManagedProcess:
        current_run = getattr(self, "_current_run", None)
        if current_run is None:
            raise RuntimeError("internal error: current run is not set")
        print(f"[CTRL] start {role}: {' '.join(cmd)}")
        proc = ManagedProcess(
            role=role,
            cmd=cmd,
            cwd=cwd,
            stdout_path=current_run.stdout_dir / f"{role}.log",
            stderr_path=current_run.stderr_dir / f"{role}.log",
            env=env,
            ready_patterns=ready_patterns,
        )
        proc.start()
        return proc

    def start_certserver(self, run_ctx: RunContext) -> ManagedProcess:
        self._current_run = run_ctx
        command = list(self.server_cfg.get("certserver_command", ["./certserver"]))
        return self.spawn(
            role="certserver",
            cmd=command,
            cwd=self.middlebox_dir,
            env={},
            ready_patterns=["Go cert service listening"],
        )

    def start_appserver(self, run_ctx: RunContext) -> ManagedProcess:
        self._current_run = run_ctx
        command = list(self.server_cfg.get("appserver_command", []))
        if not command:
            script = PROJECT_DIR.parent.parent / "PerformanceMeasuring" / "certs_server.py"
            command = ["python3", str(script)]
        env = {
            "PYTHONUNBUFFERED": "1",
            "SERVER_RUNTIME_LOG": str(run_ctx.stdout_dir / "server_runtime.log"),
        }
        return self.spawn(
            role="server",
            cmd=command,
            cwd=PROJECT_DIR.parent.parent,
            env=env,
            ready_patterns=["Application TLS server running"],
        )

    def start_client(self, run_ctx: RunContext, mode: str, clients: int, rate: float, url: str) -> ManagedProcess:
        self._current_run = run_ctx
        command = [
            str(self.client_cfg.get("binary", "./client")),
            "-mode",
            mode,
            "-d",
            str(self.duration_s),
            "-rate",
            f"{rate:g}",
            "-servername",
            str(self.client_cfg.get("servername", "server")),
            "-trace",
            str(run_ctx.traces_dir / "client.bin"),
            "-trace-buffer-events",
            str(self.trace_buffer_events),
            f"-trace-drop-on-full={str(self.trace_drop_on_full).lower()}",
            "-continue-on-error=true",
        ]
        if mode == "persistent":
            command.extend(["-clients", str(clients)])
        else:
            command.extend(["-max-in-flight", str(self.client_cfg.get("max_in_flight", 64))])
        for header in self.client_cfg.get("headers", []):
            command.extend(["-H", str(header)])
        command.append(url)
        return self.spawn(
            role="client",
            cmd=command,
            cwd=self.middlebox_dir,
            env={},
            ready_patterns=[],
        )

    def write_metadata(self, run_ctx: RunContext, metadata: dict[str, Any]) -> None:
        (run_ctx.directory / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def convert_traces(self, run_ctx: RunContext) -> None:
        go = str(DEFAULT_GO if DEFAULT_GO.exists() else "go")
        env = os.environ.copy()
        if DEFAULT_GO.exists():
            env["GOROOT"] = str(DEFAULT_GO.parents[1])
            env["PATH"] = str(DEFAULT_GO.parent) + os.pathsep + env.get("PATH", "")
            env["GOTOOLCHAIN"] = "local"

        for trace_file in run_ctx.traces_dir.rglob("*.bin"):
            rel = trace_file.relative_to(run_ctx.traces_dir)
            out = run_ctx.csv_dir / rel.with_suffix(".csv")
            out.parent.mkdir(parents=True, exist_ok=True)
            if trace_file.stat().st_size == 0:
                print(f"[CTRL] skip empty trace: {trace_file}")
                continue
            cmd = [go, "run", "./cmd/tracecsv", "-in", str(trace_file), "-out", str(out)]
            result = subprocess.run(
                cmd,
                cwd=self.middlebox_dir,
                env=env,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"[CTRL] trace conversion failed for {trace_file}: {result.stderr}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run middlebox benchmark campaigns")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "configs.yml",
        help="YAML config file",
    )
    args = parser.parse_args()

    controller = Controller(args.config)
    controller.run()


if __name__ == "__main__":
    main()
