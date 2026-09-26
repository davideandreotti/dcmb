#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
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
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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
    event_patterns: list[str] = field(default_factory=list)

    proc: subprocess.Popen[str] | None = None
    started_ns: int | None = None
    ready_ns: int | None = None
    ready_line: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    _ready_event: threading.Event = field(default_factory=threading.Event)
    _event_lock: threading.Lock = field(default_factory=threading.Lock)
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
                stripped = line.strip()
                handle.write(line)
                if self.event_patterns:
                    for pattern in self.event_patterns:
                        if pattern in line:
                            with self._event_lock:
                                self.events.append(
                                    {
                                        "pattern": pattern,
                                        "ts_ns": now_ns(),
                                        "line": stripped,
                                    }
                                )
                if self.ready_patterns and not self._ready_event.is_set():
                    if any(pattern in line for pattern in self.ready_patterns):
                        self.ready_ns = now_ns()
                        self.ready_line = stripped
                        self._ready_event.set()

    def wait_ready(self, timeout_s: float) -> None:
        if not self.ready_patterns:
            return
        if self._ready_event.wait(timeout_s):
            return
        if self.proc and self.proc.poll() is not None:
            raise RuntimeError(f"{self.role} exited before readiness; returncode={self.proc.returncode}")
        raise TimeoutError(f"{self.role} did not print readiness within {timeout_s}s")

    def wait_event(self, pattern: str, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._event_lock:
                for event in self.events:
                    if event.get("pattern") == pattern:
                        return event
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"{self.role} exited before event {pattern!r}; returncode={self.proc.returncode}")
            time.sleep(0.05)
        raise TimeoutError(f"{self.role} did not print event {pattern!r} within {timeout_s}s")

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

    def stop(self, grace_s: float = 5.0, first_signal: signal.Signals = signal.SIGINT) -> int | None:
        if self.proc is None:
            return None
        try:
            if self.proc.poll() is None:
                self._signal_group(first_signal)
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
            "events": self.events,
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
                    "user_time_tree_s",
                    "system_time_tree_s",
                    "rss_bytes",
                    "vms_bytes",
                    "num_threads",
                    "rss_tree_bytes",
                    "vms_tree_bytes",
                    "num_threads_tree",
                    "children_count",
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
                        user_time_tree = cpu.user
                        system_time_tree = cpu.system
                        rss_tree = mem.rss
                        vms_tree = mem.vms
                        threads_tree = proc.num_threads()
                        children_count = 0
                        for child in proc.children(recursive=True):
                            try:
                                child_mem = child.memory_info()
                                child_cpu = child.cpu_times()
                                user_time_tree += child_cpu.user
                                system_time_tree += child_cpu.system
                                rss_tree += child_mem.rss
                                vms_tree += child_mem.vms
                                threads_tree += child.num_threads()
                                children_count += 1
                            except psutil.Error:
                                continue
                        writer.writerow(
                            {
                                "ts_ns": ts,
                                "role": role,
                                "pid": proc.pid,
                                "returncode": managed.poll(),
                                "user_time_s": cpu.user,
                                "system_time_s": cpu.system,
                                "user_time_tree_s": user_time_tree,
                                "system_time_tree_s": system_time_tree,
                                "rss_bytes": mem.rss,
                                "vms_bytes": mem.vms,
                                "num_threads": proc.num_threads(),
                                "rss_tree_bytes": rss_tree,
                                "vms_tree_bytes": vms_tree,
                                "num_threads_tree": threads_tree,
                                "children_count": children_count,
                            }
                        )
                    except psutil.Error:
                        continue
                handle.flush()
                time.sleep(self.interval_s)


class SGXEPCMonitor:
    CSV_HEADER = (
        "timestamp_monotonic_ns,ewb_success_total,eldu_success_total,"
        "epc_total_pages,epc_free_pages,epc_occupied_pages,epc_occupied_bytes"
    )

    def __init__(self, run: "RunContext", interval_ms: int, readiness_timeout_s: float) -> None:
        self.run = run
        self.interval_ms = interval_ms
        self.readiness_timeout_s = readiness_timeout_s
        self.total_bytes = self._read_total_bytes()
        self.total_pages = self.total_bytes // 4096
        self.clock_offset_ns = time.time_ns() - time.monotonic_ns()
        self.proc: ManagedProcess | None = None

    @staticmethod
    def _read_total_bytes() -> int:
        paths = sorted(Path("/sys/devices/system/node").glob("node*/x86/sgx_total_bytes"))
        values = [int(path.read_text(encoding="utf-8").strip()) for path in paths]
        total = sum(values)
        if total <= 0:
            raise RuntimeError("could not determine total EPC bytes from sysfs")
        return total

    def start(self) -> None:
        bpftrace = shutil.which("bpftrace")
        if not bpftrace:
            raise RuntimeError("SGX EPC monitoring requested but bpftrace was not found")

        command = [bpftrace, "-q", "-B", "line", "-e", self._program()]
        if os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo:
                raise RuntimeError("SGX EPC monitoring requires root, but sudo was not found")
            command = [sudo, "-n", *command]

        self.proc = ManagedProcess(
            role="sgx_epc_monitor",
            cmd=command,
            cwd=PROJECT_DIR,
            stdout_path=self.run.epc_dir / "sgx_epc.csv",
            stderr_path=self.run.stderr_dir / "sgx_epc_monitor.log",
            ready_patterns=["timestamp_monotonic_ns,ewb_success_total"],
        )
        self.proc.start()
        try:
            self.proc.wait_ready(self.readiness_timeout_s)
        except Exception as exc:
            self.proc.join_readers()
            try:
                detail = self.proc.stderr_path.read_text(encoding="utf-8").strip()
            except OSError:
                detail = ""
            raise RuntimeError(f"SGX EPC monitor failed to start: {detail or exc}") from exc

    def stop(self) -> None:
        if self.proc:
            self.proc.stop(grace_s=5)
            self._clean_csv()

    def _clean_csv(self) -> None:
        """Remove bpftrace's automatic map dump on shutdown from the CSV."""
        path = self.run.epc_dir / "sgx_epc.csv"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        data_row = re.compile(r"^\d+,\d+,\d+,\d+,-?\d+,-?\d+,-?\d+$")
        kept = [line for line in lines if line == self.CSV_HEADER or data_row.fullmatch(line)]
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    def metadata(self) -> dict[str, Any]:
        return {
            "scope": "system_wide",
            "interval_ms": self.interval_ms,
            "epc_total_bytes": self.total_bytes,
            "epc_total_pages": self.total_pages,
            "monotonic_to_realtime_offset_ns": self.clock_offset_ns,
            "process": self.proc.metadata() if self.proc else None,
        }

    def _program(self) -> str:
        return f"""
kretprobe:__sgx_encl_ewb
/retval == 0/
{{
    @ewb_success++;
}}

kretprobe:__sgx_encl_eldu
/retval == 0/
{{
    @eldu_success++;
}}

interval:ms:{self.interval_ms}
{{
    @sample_count++;
    if (@sample_count == 1) {{
        printf("{self.CSV_HEADER}\\n");
    }}
    $free = *(int64 *)kaddr("sgx_nr_free_pages");
    printf("%llu,%llu,%llu,%llu,%lld,%lld,%lld\\n",
           nsecs, @ewb_success, @eldu_success, {self.total_pages}, $free,
           {self.total_pages} - $free, ({self.total_pages} - $free) * 4096);
}}
""".strip()


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
        collector: str = "auto",
    ) -> None:
        self.runtime = runtime
        self.names = names
        self.prefixes = prefixes
        self.csv_path = csv_path
        self.total_csv_path = total_csv_path
        self.interval_s = interval_s
        self.scope = scope
        self.collector = collector
        self.collector_source = "not_started"
        self.stop_event = threading.Event()
        self.stream_proc: subprocess.Popen[str] | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not shutil.which(self.runtime):
            print(f"[WARN] {self.runtime} not found; container stats disabled")
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.stream_proc and self.stream_proc.poll() is None:
            try:
                os.killpg(self.stream_proc.pid, signal.SIGINT)
                self.stream_proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.stream_proc.poll() is None:
                    try:
                        os.killpg(self.stream_proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.stream_proc.wait(timeout=2)
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def metadata(self) -> dict[str, str]:
        return {
            "runtime": self.runtime,
            "requested": self.collector,
            "source": self.collector_source,
            "scope": self.scope,
        }

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

    def _first_stat_value(self, item: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
        return ""

    def _write_frame(
        self,
        items: list[dict[str, Any]],
        ts: int,
        writer: csv.DictWriter,
        total_writer: csv.DictWriter,
    ) -> int:
        container_count = 0
        cpu_sum = 0.0
        mem_sum = 0.0
        pids_sum = 0
        for item in items:
            name = str(self._first_stat_value(item, "Name", "name"))
            if not self._include(name):
                continue
            container_id = self._first_stat_value(item, "Container", "ID", "Id", "id")
            cpu_perc = self._first_stat_value(item, "CPUPerc", "CPU", "CPUPercent", "cpu_percent")
            mem_usage = self._first_stat_value(item, "MemUsage", "MemUsageBytes", "mem_usage")
            mem_perc = self._first_stat_value(item, "MemPerc", "MemPercent", "mem_percent")
            net_io = self._first_stat_value(item, "NetIO", "NetInput", "net_io")
            block_io = self._first_stat_value(item, "BlockIO", "BlockInput", "block_io")
            pids = self._first_stat_value(item, "PIDs", "PIDS", "pids")
            container_count += 1
            cpu_sum += self._parse_percent(cpu_perc)
            mem_sum += self._parse_size_bytes(mem_usage)
            pids_sum += self._parse_int(pids)
            writer.writerow(
                {
                    "ts_ns": ts,
                    "runtime": self.runtime,
                    "name": name,
                    "id": container_id,
                    "cpu_perc": cpu_perc,
                    "mem_usage": mem_usage,
                    "mem_perc": mem_perc,
                    "net_io": net_io,
                    "block_io": block_io,
                    "pids": pids,
                }
            )
        if container_count == 0:
            return 0
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
        return container_count

    def _run_stats_stream(self, writer: csv.DictWriter, total_writer: csv.DictWriter) -> bool:
        interval = max(1, round(self.interval_s))
        cmd = [self.runtime, "stats"]
        if self.runtime == "podman":
            cmd.extend(["--all", "--interval", str(interval)])
        cmd.extend(["--format", "{{json .}}"])
        try:
            self.stream_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            print(f"[WARN] {self.runtime} stats stream failed to start: {exc}")
            return False

        assert self.stream_proc.stdout is not None
        frame: dict[str, dict[str, Any]] = {}
        frame_started = time.monotonic()
        samples = 0
        for line in self.stream_proc.stdout:
            line = ANSI_ESCAPE_RE.sub("", line).strip()
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            now = time.monotonic()
            if frame and now - frame_started >= interval:
                samples += self._write_frame(list(frame.values()), now_ns(), writer, total_writer)
                frame = {}
                frame_started = now
            name = str(self._first_stat_value(item, "Name", "name"))
            if name:
                frame[name] = item

        if frame:
            samples += self._write_frame(list(frame.values()), now_ns(), writer, total_writer)
        return samples > 0

    def _docker_cgroup_path(self, container_id: str) -> Path | None:
        candidates = [
            Path("/sys/fs/cgroup/system.slice") / f"docker-{container_id}.scope",
            Path("/sys/fs/cgroup/docker") / container_id,
        ]
        return next((path for path in candidates if path.is_dir()), None)

    def _docker_cgroup_paths(self) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        systemd_root = Path("/sys/fs/cgroup/system.slice")
        if systemd_root.is_dir():
            for path in systemd_root.glob("docker-*.scope"):
                paths[path.name.removeprefix("docker-").removesuffix(".scope")] = path
        cgroupfs_root = Path("/sys/fs/cgroup/docker")
        if cgroupfs_root.is_dir():
            for path in cgroupfs_root.iterdir():
                if path.is_dir():
                    paths[path.name] = path
        return paths

    def _docker_running_containers(self) -> dict[str, str]:
        result = subprocess.run(
            [self.runtime, "ps", "--no-trunc", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {}
        containers: dict[str, str] = {}
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            container_id = str(item.get("ID", ""))
            name = str(item.get("Names", ""))
            if container_id and name and self._include(name):
                containers[container_id] = name
        return containers

    def _run_docker_cgroup_v2(self, writer: csv.DictWriter, total_writer: csv.DictWriter) -> bool:
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            return False

        previous_cpu: dict[str, tuple[int, int]] = {}
        containers: dict[str, str] = {}
        next_refresh = 0.0
        samples = 0
        while not self.stop_event.is_set():
            monotonic_now = time.monotonic()
            if monotonic_now >= next_refresh:
                try:
                    containers = self._docker_running_containers()
                except subprocess.TimeoutExpired:
                    containers = {}
                next_refresh = monotonic_now + 1.0

            timestamp = now_ns()
            monotonic_ns = time.monotonic_ns()
            items: list[dict[str, Any]] = []
            active_ids: set[str] = set()
            if self.scope == "all":
                targets = [
                    (container_id, containers.get(container_id, container_id), cgroup)
                    for container_id, cgroup in self._docker_cgroup_paths().items()
                ]
            else:
                targets = []
                for container_id, name in containers.items():
                    cgroup = self._docker_cgroup_path(container_id)
                    if cgroup is not None:
                        targets.append((container_id, name, cgroup))

            for container_id, name, cgroup in targets:
                try:
                    cpu_fields = dict(
                        line.split(maxsplit=1)
                        for line in (cgroup / "cpu.stat").read_text(encoding="utf-8").splitlines()
                    )
                    usage_usec = int(cpu_fields["usage_usec"])
                    memory_bytes = int((cgroup / "memory.current").read_text(encoding="utf-8").strip())
                    pids = int((cgroup / "pids.current").read_text(encoding="utf-8").strip())
                except (FileNotFoundError, KeyError, OSError, ValueError):
                    continue

                cpu_percent = 0.0
                previous = previous_cpu.get(container_id)
                if previous is not None and monotonic_ns > previous[1]:
                    cpu_delta_usec = max(0, usage_usec - previous[0])
                    elapsed_usec = (monotonic_ns - previous[1]) / 1000.0
                    cpu_percent = cpu_delta_usec / elapsed_usec * 100.0
                previous_cpu[container_id] = (usage_usec, monotonic_ns)
                active_ids.add(container_id)
                items.append(
                    {
                        "Name": name,
                        "ID": container_id,
                        "CPUPerc": f"{cpu_percent:.6f}%",
                        "MemUsage": str(memory_bytes),
                        "MemPerc": "",
                        "NetIO": "",
                        "BlockIO": "",
                        "PIDs": str(pids),
                    }
                )

            previous_cpu = {container_id: value for container_id, value in previous_cpu.items() if container_id in active_ids}
            samples += self._write_frame(items, timestamp, writer, total_writer)
            self.stop_event.wait(self.interval_s)
        return samples > 0

    def _run_polling(self, writer: csv.DictWriter, total_writer: csv.DictWriter) -> None:
        self.collector_source = f"{self.runtime}_stats_no_stream"
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
                items: list[dict[str, Any]] = []
                for line in result.stdout.splitlines():
                    if not line.strip():
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                self._write_frame(items, ts, writer, total_writer)
            time.sleep(self.interval_s)

    def _run(self) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8", buffering=1) as handle, self.total_csv_path.open(
            "w", newline="", encoding="utf-8", buffering=1
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
            use_cgroup = self.runtime == "docker" and self.collector in {"auto", "cgroup"}
            use_stream = self.runtime in {"docker", "podman"} and self.collector in {"auto", "stream"}
            if use_cgroup:
                self.collector_source = "docker_cgroup_v2"
                cgroup_ok = self._run_docker_cgroup_v2(writer, total_writer)
                if not cgroup_ok and not self.stop_event.is_set():
                    print("[WARN] Docker cgroup-v2 collector unavailable; falling back to stats stream")
                    self.collector_source = "docker_stats_stream"
                    self._run_stats_stream(writer, total_writer)
            elif use_stream:
                self.collector_source = f"{self.runtime}_stats_stream"
                stream_ok = self._run_stats_stream(writer, total_writer)
                if not stream_ok and not self.stop_event.is_set():
                    print(f"[WARN] {self.runtime} stats stream produced no samples; falling back to --no-stream")
                    self._run_polling(writer, total_writer)
            else:
                self._run_polling(writer, total_writer)
            handle.flush()
            total_handle.flush()


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

    def uses_certserver(self) -> bool:
        return True

    def start(self) -> ManagedProcess | None:
        raise NotImplementedError

    def wait_ready(self, timeout_s: float) -> None:
        if self.proc:
            self.proc.wait_ready(timeout_s)

    def stop(self) -> None:
        if self.proc:
            self.proc.stop(grace_s=8)

    def client_url(self) -> str:
        raise NotImplementedError

    def container_monitor(self) -> ContainerStatsMonitor | None:
        return None


class DirectDeployment(Deployment):
    def uses_certserver(self) -> bool:
        return False

    def start(self) -> ManagedProcess | None:
        return None

    def client_url(self) -> str:
        return f"{self.controller.server_target_url}{self.controller.request_path}"


class BaremetalDeployment(Deployment):
    def env(self) -> dict[str, str]:
        env = {
            "OPERATOR_TARGET": self.controller.server_target_url,
            "OPERATOR_CERT_URL": self.controller.server_cert_url,
        }
        env.update({str(k): str(v) for k, v in self.cfg.get("env", {}).items()})
        return env

    def start(self) -> ManagedProcess:
        command = list(self.cfg.get("command", ["./middlebox", "-log_level", "error", "-minimal_logs=true"]))
        if self.controller.trace_enabled_for("middlebox") and bool(
            self.cfg.get("trace_enabled", True)
        ):
            command.extend(
                [
                    "-trace",
                    str(self.run.traces_dir / "middlebox.bin"),
                    "-trace-buffer-events",
                    str(self.controller.trace_buffer_events),
                    f"-trace-drop-on-full={str(self.controller.trace_drop_on_full).lower()}",
                ]
            )
        self.proc = self.controller.spawn(
            role="middlebox",
            cmd=command,
            cwd=self.controller.middlebox_dir,
            env=self.env(),
            ready_patterns=["[OPERATOR_READY]"],
        )
        return self.proc

    def client_url(self) -> str:
        host = self.controller.hosts["middlebox"].get("ip", "127.0.0.1")
        return f"https://{host}:8443{self.controller.request_path}"


class GramineSGXDeployment(BaremetalDeployment):
    def enclave_trace_path(self) -> str:
        trace_file = (self.run.traces_dir / "middlebox.bin").resolve()
        try:
            rel = trace_file.relative_to(self.controller.output_root.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "SGX trace path must live under the configured output_root mounted at /trace"
            ) from exc
        return str(PurePosixPath("/trace") / PurePosixPath(rel.as_posix()))

    def start(self) -> ManagedProcess:
        command = list(
            self.cfg.get(
                "command",
                ["gramine-sgx", "middlebox", "-log_level", "error", "-minimal_logs=true"],
            )
        )
        if self.controller.trace_enabled_for("middlebox") and bool(
            self.cfg.get("trace_enabled", True)
        ):
            command.extend(
                [
                    "-trace",
                    self.enclave_trace_path(),
                    "-trace-buffer-events",
                    str(self.controller.trace_buffer_events),
                    f"-trace-drop-on-full={str(self.controller.trace_drop_on_full).lower()}",
                ]
            )
        self.proc = self.controller.spawn(
            role="middlebox",
            cmd=command,
            cwd=self.controller.middlebox_dir,
            env=self.env(),
            ready_patterns=["[OPERATOR_READY]"],
        )
        return self.proc

    def stop(self) -> None:
        if self.proc:
            self.proc.stop(grace_s=8, first_signal=signal.SIGTERM)


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
        worker_image = str(self.cfg.get("worker_image", "dcmiddlebox-worker:baseline"))
        socket_host = resolve_socket_path(str(self.cfg.get("socket_host", default_container_api_socket(runtime))))
        socket_container = str(self.cfg.get("socket_container", "/var/run/docker.sock"))
        certs_host = str(self.cfg.get("worker_certs_host_path", PROJECT_DIR.parent.parent / "certs_external"))
        certs_container = str(self.cfg.get("worker_certs_container_path", certs_host))
        min_ready = str(self.cfg.get("min_ready", 1))
        scale_up_by = str(self.cfg.get("scale_up_by", min_ready))
        worker_trace_enabled = str(bool(self.cfg.get("worker_trace_enabled", False))).lower()
        worker_reuse_dc = str(bool(self.cfg.get("worker_reuse_dc", True))).lower()
        worker_sgx_enabled = bool(self.cfg.get("worker_sgx_enabled", False))
        traces_host = str(self.run.traces_dir.resolve())

        ensure_container_network(runtime, network, bool(self.cfg.get("create_network", True)))

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
            f"{socket_host}:{socket_container}",
            "-v",
            f"{traces_host}:/trace",
            "-e",
            "GATEWAY_BACKEND_MODE=docker",
            "-e",
            f"DOCKER_SOCKET={socket_container}",
            "-e",
            f"DOCKER_WORKER_IMAGE={worker_image}",
            "-e",
            f"DOCKER_WORKER_NETWORK={network}",
            "-e",
            f"OPERATOR_TARGET={self.controller.server_target_url}",
            "-e",
            f"OPERATOR_CERT_URL={self.controller.server_cert_url}",
            "-e",
            f"DOCKER_WORKER_CERTS_HOST_PATH={certs_host}",
            "-e",
            f"DOCKER_WORKER_CERTS_CONTAINER_PATH={certs_container}",
            "-e",
            f"DOCKER_WORKER_REUSE_DC={worker_reuse_dc}",
            "-e",
            f"DOCKER_WORKER_LOG_LEVEL={self.cfg.get('worker_log_level', 'error')}",
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
            f"DOCKER_WORKER_TRACE_BUFFER_EVENTS={self.controller.trace_buffer_events}",
            "-e",
            f"DOCKER_WORKER_TRACE_DROP_ON_FULL={str(self.controller.trace_drop_on_full).lower()}",
            "-e",
            f"DOCKER_API_TIMEOUT_MS={self.cfg.get('docker_api_timeout_ms', 5000)}",
        ]

        max_workers = self.cfg.get("max_workers")
        if max_workers is not None:
            cmd.extend(["-e", f"DOCKER_MAX_OPERATORS={int(max_workers)}"])

        if worker_sgx_enabled:
            cmd.extend(["-e", "DOCKER_WORKER_SGX_ENABLED=true"])
            cmd.extend(["-e", f"DOCKER_WORKER_SGX_ENCLAVE_DEVICE={self.cfg.get('worker_sgx_enclave_device', '/dev/sgx_enclave')}"])
            cmd.extend(["-e", f"DOCKER_WORKER_SGX_PROVISION_DEVICE={self.cfg.get('worker_sgx_provision_device', '/dev/sgx_provision')}"])
            cmd.extend(["-e", f"DOCKER_WORKER_AESM_DIR={self.cfg.get('worker_aesm_dir', '/var/run/aesmd')}"])
        if "worker_ca" in self.cfg:
            cmd.extend(["-e", f"DOCKER_WORKER_CA={self.cfg.get('worker_ca')}"])
        if "worker_emit_quote" in self.cfg:
            cmd.extend(["-e", f"DOCKER_WORKER_EMIT_QUOTE={self.cfg.get('worker_emit_quote')}"])
        if "worker_signal_timeout_ms" in self.cfg:
            cmd.extend(["-e", f"DOCKER_WORKER_SIGNAL_TIMEOUT_MS={self.cfg.get('worker_signal_timeout_ms')}"])
        if "delete_after_use" in self.cfg:
            cmd.extend(["-e", f"DOCKER_DELETE_AFTER_USE={str(bool(self.cfg.get('delete_after_use'))).lower()}"])
        if "ticket_identity_key" in self.cfg:
            cmd.extend(["-e", f"DCMB_TICKET_IDENTITY_KEY={self.cfg.get('ticket_identity_key')}"])
            if "delete_after_use" not in self.cfg:
                cmd.extend(["-e", "DOCKER_DELETE_AFTER_USE=false"])

        cmd.extend([
            "-e",
            f"DOCKER_READY_TIMEOUT_MS={self.cfg.get('docker_ready_timeout_ms', 15000)}",
            image,
            "-log_level",
            str(self.cfg.get("gateway_log_level", "error")),
        ])
        if self.controller.trace_enabled_for("gateway"):
            cmd.extend(
                [
                    "-trace",
                    "/trace/gateway.bin",
                    "-trace-buffer-events",
                    str(self.controller.trace_buffer_events),
                    f"-trace-drop-on-full={str(self.controller.trace_drop_on_full).lower()}",
                ]
            )

        self.proc = self.controller.spawn(
            role="gateway",
            cmd=cmd,
            cwd=self.controller.middlebox_dir,
            env={},
            ready_patterns=["[GATEWAY_READY]"],
            event_patterns=["docker worker ready", "[GATEWAY_POOL_READY]"],
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

    def wait_ready(self, timeout_s: float) -> None:
        super().wait_ready(timeout_s)
        min_ready = int(self.cfg.get("min_ready", 1))
        if min_ready > 0 and self.proc:
            if bool(self.cfg.get("wait_for_full_pool", False)):
                pool_timeout = float(self.cfg.get("pool_readiness_timeout_s", timeout_s))
                event = self.proc.wait_event("[GATEWAY_POOL_READY]", pool_timeout)
                print(f"[CTRL] docker worker pool ready: {event.get('line', '')}")
            else:
                event = self.proc.wait_event("docker worker ready", timeout_s)
                print(f"[CTRL] first docker worker ready: {event.get('line', '')}")

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
            collector=str(self.cfg.get("container_stats_collector", "auto")),
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

    @property
    def epc_dir(self) -> Path:
        return self.directory / "epc"

    def create(self) -> None:
        for path in [
            self.stdout_dir,
            self.stderr_dir,
            self.traces_dir,
            self.csv_dir,
            self.cpu_dir,
            self.epc_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)


def run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not cmd or not shutil.which(cmd[0]):
        return None
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)


def resolve_socket_path(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def default_container_api_socket(runtime: str) -> str:
    if runtime == "podman":
        xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
        if xdg_runtime_dir:
            return str(Path(xdg_runtime_dir) / "podman" / "podman.sock")
    return "/var/run/docker.sock"


def ensure_container_network(runtime: str, network: str, create: bool = True) -> None:
    if not network or not shutil.which(runtime):
        return

    inspected = subprocess.run(
        [runtime, "network", "inspect", network],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if inspected.returncode == 0 or not create:
        return

    created = subprocess.run(
        [runtime, "network", "create", network],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if created.returncode != 0:
        raise RuntimeError(
            f"failed to create container network {network!r} with {runtime}: {created.stderr.strip()}"
        )


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
        self.sgx_epc_monitor_enabled = bool(
            self.campaign.get("sgx_epc_monitor_enabled", False)
        )
        self.sgx_epc_interval_ms = int(
            self.campaign.get("sgx_epc_interval_ms", 1000)
        )
        if self.sgx_epc_interval_ms <= 0:
            raise ValueError("campaign.sgx_epc_interval_ms must be positive")
        trace_roles = self.campaign.get("trace_roles")
        self.trace_roles = (
            None if trace_roles is None else {str(role) for role in trace_roles}
        )
        self.resource_sampling_enabled = bool(
            self.campaign.get("resource_sampling_enabled", True)
        )
        self.trace_buffer_events = int(self.campaign.get("trace_buffer_events", 100000))
        self.trace_drop_on_full = bool(self.campaign.get("trace_drop_on_full", True))
        self.startup_only = bool(self.campaign.get("startup_only", False))

        campaign_name = sanitize(self.campaign.get("name", "campaign"))
        self.campaign_dir = self.output_root / f"{timestamp_name()}_{campaign_name}"
        self.summary_path = self.campaign_dir / "summary.csv"

    def trace_enabled_for(self, role: str) -> bool:
        return self.trace_roles is None or role in self.trace_roles

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
                    "failure_reason",
                    "run_dir",
                ],
            )
            writer.writeheader()

            for deployment_cfg, mode, clients, rate, iteration in self.iter_matrix():
                run_name = self.run_name(deployment_cfg, mode, clients, rate, iteration)
                run_ctx = RunContext(run_name, self.campaign_dir / run_name)
                status = "ok"
                client_returncode = None
                failure_reason = ""
                try:
                    client_returncode = self.run_one(run_ctx, deployment_cfg, mode, clients, rate, iteration)
                    if client_returncode not in {None, 0}:
                        status = "failed"
                        failure_reason = f"client exited with returncode={client_returncode}"
                except Exception as exc:
                    status = "failed"
                    failure_reason = str(exc)
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
                        "failure_reason": failure_reason,
                        "run_dir": str(run_ctx.directory),
                    }
                )
                summary.flush()
                if self.cooldown_s > 0:
                    time.sleep(self.cooldown_s)

    def iter_matrix(self) -> Any:
        matrix = self.config.get("client_matrix", {})
        deployments = self.config.get("deployments", [])
        if self.startup_only:
            for deployment_cfg in deployments:
                for iteration in range(1, self.runs + 1):
                    yield deployment_cfg, "startup", 0, 0.0, iteration
            return
        default_modes = matrix.get("modes", ["fresh"])
        default_clients_values = matrix.get("clients", [1])
        default_rates = matrix.get("rates", [10])
        default_rates_by_mode = matrix.get("rates_by_mode", {})
        default_points = matrix.get("points")
        for deployment_cfg in deployments:
            modes = deployment_cfg.get("modes", default_modes)
            clients_values = deployment_cfg.get("clients", default_clients_values)
            deployment_rates_by_mode = deployment_cfg.get("rates_by_mode", default_rates_by_mode)
            points = deployment_cfg.get("points", default_points)
            for mode in modes:
                if points is not None:
                    for point in points:
                        clients = int(point["clients"])
                        rate = float(point["rate"])
                        if str(mode) == "fresh" and rate <= 0:
                            continue
                        for iteration in range(1, self.runs + 1):
                            yield deployment_cfg, str(mode), clients, rate, iteration
                    continue
                rates = deployment_cfg.get(
                    "rates",
                    deployment_rates_by_mode.get(str(mode), default_rates),
                )
                for clients, rate in itertools.product(clients_values, rates):
                    if str(mode) == "fresh" and float(rate) <= 0:
                        continue
                    for iteration in range(1, self.runs + 1):
                        yield deployment_cfg, str(mode), int(clients), float(rate), iteration

    def run_name(self, deployment_cfg: dict[str, Any], mode: str, clients: int, rate: float, iteration: int) -> str:
        if self.startup_only:
            return sanitize(f"{deployment_cfg['name']}_startup_run{iteration}")
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
        self._current_run = run_ctx
        effective_deployment_cfg = deep_copy_jsonable(deployment_cfg)
        if (
            effective_deployment_cfg.get("kind") == "docker_gateway"
            and bool(effective_deployment_cfg.get("prewarm_for_clients", False))
            and mode != "startup"
        ):
            warmup_spare = 1 if mode == "persistent" else 0
            initial_workers = 1 if mode == "fresh" else clients + warmup_spare
            effective_deployment_cfg["min_ready"] = initial_workers
            effective_deployment_cfg["max_workers"] = initial_workers
            effective_deployment_cfg["wait_for_full_pool"] = True

        deployment = self.make_deployment(effective_deployment_cfg, run_ctx)
        deployment.cleanup_before()

        metadata: dict[str, Any] = {
            "run_name": run_ctx.name,
            "parameters": {
                "deployment": effective_deployment_cfg,
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
        sgx_epc_monitor: SGXEPCMonitor | None = None
        client_proc: ManagedProcess | None = None

        try:
            if not self.startup_only and deployment.uses_certserver():
                certserver = self.start_certserver(run_ctx)
                managed.append(("certserver", certserver))
                certserver.wait_ready(self.readiness_timeout_s)

            if not self.startup_only:
                appserver = self.start_appserver(run_ctx)
                managed.append(("server", appserver))
                appserver.wait_ready(self.readiness_timeout_s)

            if self.sgx_epc_monitor_enabled and self.deployment_uses_sgx(
                effective_deployment_cfg
            ):
                sgx_epc_monitor = SGXEPCMonitor(
                    run_ctx,
                    self.sgx_epc_interval_ms,
                    self.readiness_timeout_s,
                )
                sgx_epc_monitor.start()

            deployment_proc = deployment.start()
            if deployment_proc:
                managed.append((deployment.kind, deployment_proc))
                deployment.wait_ready(self.readiness_timeout_s)

            if self.startup_only:
                return 0

            if self.warmup_s > 0:
                time.sleep(self.warmup_s)

            if self.resource_sampling_enabled:
                cpu_monitor = ProcessCPUMonitor(
                    managed, run_ctx.cpu_dir / "processes.csv", self.cpu_interval_s
                )
                cpu_monitor.start()
                container_monitor = deployment.container_monitor()
                if container_monitor:
                    container_monitor.start()

            client_proc = self.start_client(run_ctx, mode, clients, rate, deployment.client_url())
            managed.append(("client", client_proc))
            self.wait_for_client_or_deployment(client_proc, deployment, self.duration_s + 30)
            returncode = client_proc.poll()
            print(f"[CTRL] client finished returncode={returncode}")
            if returncode not in {None, 0}:
                metadata["failure_reason"] = f"client exited with returncode={returncode}"
            return returncode
        except Exception as exc:
            metadata["failure_reason"] = str(exc)
            raise
        finally:
            if client_proc and client_proc.poll() is None:
                client_proc.stop(grace_s=5)
            if cpu_monitor:
                cpu_monitor.stop()
            if container_monitor:
                container_monitor.stop()
                metadata["resource_collector"] = container_monitor.metadata()
            if sgx_epc_monitor:
                sgx_epc_monitor.stop()
                metadata["sgx_epc_monitor"] = sgx_epc_monitor.metadata()
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
            self.write_startup_csv(run_ctx, managed)
            self.convert_traces(run_ctx)

    @staticmethod
    def deployment_uses_sgx(cfg: dict[str, Any]) -> bool:
        return cfg.get("kind") == "gramine_sgx" or bool(cfg.get("worker_sgx_enabled", False))

    def wait_for_client_or_deployment(
        self,
        client_proc: ManagedProcess,
        deployment: Deployment,
        timeout_s: float,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if deployment.proc and deployment.proc.poll() is not None:
                raise RuntimeError(
                    f"deployment {deployment.name!r} exited early; "
                    f"returncode={deployment.proc.poll()}"
                )
            if client_proc.poll() is not None:
                client_proc.join_readers()
                return
            time.sleep(0.1)
        raise TimeoutError(f"client did not finish within {timeout_s:.1f}s")

    def make_deployment(self, cfg: dict[str, Any], run_ctx: RunContext) -> Deployment:
        kind = str(cfg.get("kind"))
        if kind == "direct":
            return DirectDeployment(cfg, self, run_ctx)
        if kind == "baremetal":
            return BaremetalDeployment(cfg, self, run_ctx)
        if kind == "gramine_sgx":
            return GramineSGXDeployment(cfg, self, run_ctx)
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
        event_patterns: list[str] | None = None,
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
            event_patterns=event_patterns or [],
        )
        proc.start()
        return proc

    def start_certserver(self, run_ctx: RunContext) -> ManagedProcess:
        self._current_run = run_ctx
        command = list(self.server_cfg.get("certserver_command", ["./certserver"]))
        if self.trace_enabled_for("certserver"):
            command.extend(
                [
                    "-trace",
                    str(run_ctx.traces_dir / "certserver.bin"),
                    "-trace-buffer-events",
                    str(self.trace_buffer_events),
                    f"-trace-drop-on-full={str(self.trace_drop_on_full).lower()}",
                ]
            )
        return self.spawn(
            role="certserver",
            cmd=command,
            cwd=self.middlebox_dir,
            env={},
            ready_patterns=["Go cert service listening"],
        )

    def start_appserver(self, run_ctx: RunContext) -> ManagedProcess:
        self._current_run = run_ctx
        command = list(self.server_cfg.get("appserver_command", ["./appserver"]))
        if self.trace_enabled_for("server"):
            command.extend(
                [
                    "-trace",
                    str(run_ctx.traces_dir / "server.bin"),
                    "-trace-buffer-events",
                    str(self.trace_buffer_events),
                    f"-trace-drop-on-full={str(self.trace_drop_on_full).lower()}",
                ]
            )
        return self.spawn(
            role="server",
            cmd=command,
            cwd=self.middlebox_dir,
            env={},
            ready_patterns=["[REQUEST_SERVER_READY]"],
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
            "-pacing",
            str(self.client_cfg.get("pacing", "spin")),
            "-servername",
            str(self.client_cfg.get("servername", "server")),
            "-continue-on-error=true",
            "-log_level",
            str(self.client_cfg.get("log_level", "error")),
        ]
        if self.trace_enabled_for("client"):
            command.extend(
                [
                    "-trace",
                    str(run_ctx.traces_dir / "client.bin"),
                    "-trace-buffer-events",
                    str(self.trace_buffer_events),
                    f"-trace-drop-on-full={str(self.trace_drop_on_full).lower()}",
                ]
            )
        if mode in {"persistent", "resumption"}:
            command.extend(["-clients", str(clients)])
        else:
            command.extend(["-max-in-flight", str(self.client_cfg.get("max_in_flight", 64))])
        data = self.client_cfg.get("data", "")
        if data:
            if isinstance(data, (dict, list)):
                data = json.dumps(data, separators=(",", ":"))
            command.extend(["-data", str(data)])
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

    def write_startup_csv(self, run_ctx: RunContext, managed: list[tuple[str, ManagedProcess]]) -> None:
        path = run_ctx.csv_dir / "startup.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "role",
            "metric",
            "name",
            "kind",
            "start_ns",
            "ready_ns",
            "startup_ns",
            "startup_ms",
            "source",
            "detail",
        ]

        rows: list[dict[str, Any]] = []
        for kind, proc in managed:
            if proc.role == "client" or proc.ready_ns is None or proc.started_ns is None:
                continue
            startup_ns = proc.ready_ns - proc.started_ns
            rows.append(
                {
                    "role": proc.role,
                    "metric": "process_ready",
                    "name": proc.role,
                    "kind": kind,
                    "start_ns": proc.started_ns,
                    "ready_ns": proc.ready_ns,
                    "startup_ns": startup_ns,
                    "startup_ms": f"{startup_ns / 1_000_000:.3f}",
                    "source": "controller_ready_line",
                    "detail": proc.ready_line,
                }
            )

            if proc.role != "gateway":
                continue

            first_worker_event: dict[str, Any] | None = None
            for event in proc.events:
                if event.get("pattern") != "docker worker ready":
                    continue
                line = str(event.get("line", ""))
                match = re.search(r"name=(\S+).*startup_ms=(\d+(?:\.\d+)?)", line)
                worker_name = match.group(1) if match else ""
                worker_startup_ms = match.group(2) if match else ""

                if first_worker_event is None:
                    first_worker_event = event
                    if proc.started_ns is not None:
                        ready_ns = int(event["ts_ns"])
                        startup_ns = ready_ns - proc.started_ns
                        rows.append(
                            {
                                "role": "gateway",
                                "metric": "first_worker_ready",
                                "name": worker_name,
                                "kind": kind,
                                "start_ns": proc.started_ns,
                                "ready_ns": ready_ns,
                                "startup_ns": startup_ns,
                                "startup_ms": f"{startup_ns / 1_000_000:.3f}",
                                "source": "controller_log_event",
                                "detail": line,
                            }
                        )

                rows.append(
                    {
                        "role": "worker_container",
                        "metric": "worker_ready_internal",
                        "name": worker_name,
                        "kind": "docker_worker",
                        "start_ns": "",
                        "ready_ns": event.get("ts_ns", ""),
                        "startup_ns": "",
                        "startup_ms": worker_startup_ms,
                        "source": "gateway_log_startup_ms",
                        "detail": line,
                    }
                )

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

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
