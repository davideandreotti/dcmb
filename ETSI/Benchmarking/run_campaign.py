#!/usr/bin/env python3

import argparse
import csv
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from run_latency import merge_application_trace


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = Path(__file__).resolve().parent
TLMSP_PREFIX = ROOT / ".tlmsp"
FULL_CONFIG = ROOT / "ETSI/Configurations/local_init.ucl"
NO_HANDLER_CONFIG = ROOT / "ETSI/Configurations/local_init_no_handler.ucl"
MIDDLEBOX_DIR = ROOT / "ETSI/NewMiddlebox"
APP_SERVER = ROOT / "DC/Middlebox/appserver"
CUSTOM_GO = ROOT / "DC/go/bin/go"
LOADGEN = BENCHMARK_DIR / "tlmsp_loadgen"
RUN_LATENCY = BENCHMARK_DIR / "run_latency.py"
URL = "https://127.0.0.1:4444/function/init"
SUMMARY_FIELDS = [
    "experiment", "profile", "mode", "rate", "run", "duration_s",
    "scheduled", "launched", "completed", "successes", "errors",
    "missed", "achieved_rps", "max_in_flight", "valid",
    "middlebox_tree_cpu_percent", "cpu_scope", "samples_csv",
    "events_log", "component_logs",
]


def parse_key_values(line, prefix):
    if not line.startswith(prefix):
        return None
    values = {}
    for item in line[len(prefix):].strip().split():
        if "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def tlmsp_environment(trace=False):
    environment = os.environ.copy()
    environment["PATH"] = str(TLMSP_PREFIX / "bin") + ":" + environment.get("PATH", "")
    environment["LD_LIBRARY_PATH"] = (
        str(TLMSP_PREFIX / "lib") + ":" + environment.get("LD_LIBRARY_PATH", ""))
    environment["TLMSP_UCL"] = str(TLMSP_PREFIX / "share/tlmsp-tools/examples")
    environment["TLMSP_BENCH_TRACE"] = "1" if trace else "0"
    return environment


def listening_ports():
    ports = set()
    for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) >= 4 and fields[3] == "0A":
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


class ManagedProcess:
    def __init__(self, name, command, cwd, log_dir, environment):
        self.name = name
        self.command = [str(item) for item in command]
        self.cwd = Path(cwd)
        self.environment = environment
        self.stdout_path = log_dir / f"{name}.stdout.log"
        self.stderr_path = log_dir / f"{name}.stderr.log"
        self.process = None
        self.stdout_file = None
        self.stderr_file = None

    def start(self):
        self.stdout_file = self.stdout_path.open("w", encoding="utf-8")
        self.stderr_file = self.stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            self.command, cwd=self.cwd, env=self.environment,
            stdout=self.stdout_file, stderr=self.stderr_file,
            text=True, start_new_session=True)

    def wait_listening(self, port, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.name} exited with status {self.process.returncode}; "
                    f"see {self.stderr_path}")
            if port in listening_ports():
                return
            time.sleep(0.05)
        raise RuntimeError(f"{self.name} did not listen on port {port}")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
            except ProcessLookupError:
                pass
        if self.stdout_file is not None:
            self.stdout_file.close()
        if self.stderr_file is not None:
            self.stderr_file.close()


def read_process_cpu(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    fields = text.rsplit(")", 1)[1].split()
    if len(fields) < 15:
        return None
    ticks = os.sysconf("SC_CLK_TCK")
    own = (int(fields[11]) + int(fields[12])) / ticks
    children = (int(fields[13]) + int(fields[14])) / ticks
    return own, children


class CPUMonitor:
    def __init__(self, process, output, interval):
        self.process = process
        self.output = output
        self.interval = interval
        self.samples = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self):
        while not self.stop_event.is_set():
            value = read_process_cpu(self.process.pid)
            if value is not None:
                self.samples.append((time.time_ns(), value[0], value[1]))
            self.stop_event.wait(self.interval)
        value = read_process_cpu(self.process.pid)
        if value is not None:
            self.samples.append((time.time_ns(), value[0], value[1]))

    def write(self):
        with self.output.open("w", newline="", encoding="utf-8") as target:
            writer = csv.writer(target)
            writer.writerow([
                "ts_ns", "middlebox_cpu_s", "reaped_children_cpu_s",
                "middlebox_tree_cpu_s",
            ])
            for timestamp, own, children in self.samples:
                writer.writerow([
                    timestamp, f"{own:.6f}", f"{children:.6f}",
                    f"{own + children:.6f}",
                ])

    def value_at(self, timestamp):
        if not self.samples:
            return None
        if timestamp <= self.samples[0][0]:
            return sum(self.samples[0][1:])
        if timestamp >= self.samples[-1][0]:
            return sum(self.samples[-1][1:])
        for left, right in zip(self.samples, self.samples[1:]):
            if left[0] <= timestamp <= right[0]:
                fraction = (timestamp - left[0]) / (right[0] - left[0])
                left_cpu = left[1] + left[2]
                right_cpu = right[1] + right[2]
                return left_cpu + fraction * (right_cpu - left_cpu)
        return None

    def percent_between(self, start_ns, done_ns):
        start_cpu = self.value_at(start_ns)
        done_cpu = self.value_at(done_ns)
        if start_cpu is None or done_cpu is None or done_ns <= start_ns:
            return None
        return max(0.0, done_cpu - start_cpu) * 100.0 / (
            (done_ns - start_ns) / 1_000_000_000)


class TLMSPStack:
    def __init__(self, profile, log_dir, readiness_timeout, trace_application):
        self.profile = profile
        self.log_dir = log_dir
        self.readiness_timeout = readiness_timeout
        self.trace_application = trace_application
        self.processes = []
        self.middlebox = None
        self.appserver_trace = self.log_dir / "appserver.trace.bin"

    @property
    def config(self):
        return FULL_CONFIG if self.profile == "full" else NO_HANDLER_CONFIG

    def _start(self, name, command, cwd, port, trace=False):
        process = ManagedProcess(
            name, command, cwd, self.log_dir, tlmsp_environment(trace))
        process.start()
        self.processes.append(process)
        process.wait_listening(port, self.readiness_timeout)
        return process

    def start(self):
        required_ports = {7000, 4443, 4444, 10001}
        if self.profile == "full":
            required_ports.add(8080)
        occupied = required_ports & listening_ports()
        if occupied:
            raise RuntimeError(
                "required ports already in use: " +
                ", ".join(str(port) for port in sorted(occupied)))

        appserver_command = [
            APP_SERVER, "-addr", "127.0.0.1:7000", "-tls=false",
            "-log_level", "error",
        ]
        if self.trace_application:
            appserver_command.extend([
                "-trace", self.appserver_trace, "-trace-drop-on-full=false",
            ])
        self._start(
            "appserver", appserver_command, ROOT / "DC/Middlebox", 7000)
        self._start(
            "apache", [TLMSP_PREFIX / "bin/httpd", "-X", "-e", "warn",
            "-c", "KeepAlive On", "-c", "MaxKeepAliveRequests 0"],
            ROOT, 4444)
        if self.profile == "full":
            self._start(
                "listener", [MIDDLEBOX_DIR / "listener"],
                MIDDLEBOX_DIR, 8080)

        waiting = MIDDLEBOX_DIR / "waiting.dat"
        waiting.unlink(missing_ok=True)
        self.middlebox = self._start(
            "middlebox", [TLMSP_PREFIX / "bin/tlmsp-mb", "-c", self.config,
            "-a"], MIDDLEBOX_DIR, 10001, trace=True)

    def stop(self):
        for process in reversed(self.processes):
            process.stop()


def isolated_warmup(config, output):
    command = [
        str(LOADGEN), "--mode", "fresh", "--config", str(config),
        "--url", URL, "--warmup", "0", "--requests", "1",
        "--interval-ms", "0",
    ]
    with output.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            command, cwd=MIDDLEBOX_DIR, env=tlmsp_environment(False),
            stdout=subprocess.DEVNULL, stderr=log, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"isolated warmup failed; see {output}")


def convert_application_trace(trace_path):
    if not trace_path.exists() or trace_path.stat().st_size == 0:
        raise RuntimeError(
            "application-server trace is missing; rebuild appserver with "
            "DC/Middlebox/compile.sh")
    output = trace_path.with_suffix(".csv")
    go = str(CUSTOM_GO if CUSTOM_GO.exists() else "go")
    environment = os.environ.copy()
    if CUSTOM_GO.exists():
        environment["GOROOT"] = str(CUSTOM_GO.parents[1])
        environment["PATH"] = str(CUSTOM_GO.parent) + os.pathsep + environment.get("PATH", "")
        environment["GOTOOLCHAIN"] = "local"
    completed = subprocess.run(
        [go, "run", "./cmd/tracecsv", "-in", str(trace_path),
         "-out", str(output)],
        cwd=ROOT / "DC/Middlebox", env=environment,
        capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "application-server trace conversion failed: " +
            completed.stderr.strip())
    return output


def parse_raw_events(path):
    summary = None
    window = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_key_values(line, "TLMSP_SUMMARY ")
        if parsed:
            summary = parsed
        parsed = parse_key_values(line, "TLMSP_WINDOW ")
        if parsed:
            window[parsed["event"]] = int(parsed["ts_ns"])
    if summary is None or "start" not in window or "done" not in window:
        raise RuntimeError(f"run summary or timing window missing from {path}")
    return summary, window


def rate_label(rate):
    return f"{rate:g}".replace(".", "p")


def default_output():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return BENCHMARK_DIR / "results" / f"tlmsp_{stamp}"


def planned_points(args):
    if args.experiment == "latency":
        profiles = args.profiles or ["full", "no_handler"]
        for profile in profiles:
            yield profile, "fresh", args.fresh_rate
            yield profile, "persistent", args.persistent_rate
    elif args.experiment == "throughput":
        profiles = args.profiles or ["full"]
        for profile in profiles:
            for rate in args.rates:
                yield profile, "persistent", rate
    else:
        for rate in args.rates:
            yield "no_handler", "fresh", rate


def point_is_valid(summary):
    scheduled = int(summary["scheduled"])
    launched = int(summary["launched"])
    completed = int(summary["completed"])
    successes = int(summary["successes"])
    errors = int(summary["errors"])
    allowed_deficit = max(1, math.ceil(scheduled * 0.02))
    return (
        errors == 0 and completed == launched and
        scheduled - successes <= allowed_deficit)


def run_point(args, profile, mode, rate, run_number, output_dir):
    profile_dir = output_dir / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    if args.experiment == "latency":
        stem = f"{mode}_run{run_number:02d}"
    else:
        stem = f"{mode}_rate{rate_label(rate)}_run{run_number:02d}"
    samples_path = profile_dir / f"{stem}.csv"
    events_path = samples_path.with_suffix(".events.log")
    cpu_path = profile_dir / f"{stem}.cpu.csv"
    log_dir = profile_dir / "logs" / stem
    log_dir.mkdir(parents=True, exist_ok=True)

    trace_application = args.experiment == "latency" and profile == "full"
    stack = TLMSPStack(
        profile, log_dir, args.readiness_timeout_seconds, trace_application)
    print(
        f"[{args.experiment}] profile={profile} mode={mode} "
        f"rate={rate:g} run={run_number}/{args.runs}", flush=True)
    result = None
    try:
        stack.start()
        time.sleep(args.warmup_seconds)
        isolated_warmup(stack.config, log_dir / "isolated_warmup.log")
        time.sleep(args.post_warmup_pause_seconds)

        command = [
            sys.executable, str(RUN_LATENCY), mode,
            "--duration-seconds", str(args.duration_seconds),
            "--rate", str(rate),
            "--warmup", "1" if mode == "persistent" else "0",
            "--warmup-pause-ms",
            str(int(args.persistent_prime_pause_seconds * 1000))
            if mode == "persistent" else "0",
            "--max-in-flight", str(args.max_in_flight),
            "--middlebox-log", str(stack.middlebox.stderr_path),
            "--config", str(stack.config),
            "--output", str(samples_path),
        ]
        monitor = CPUMonitor(stack.middlebox.process, cpu_path, args.cpu_interval_seconds)
        monitor.start()
        with (log_dir / "runner.stdout.log").open("w", encoding="utf-8") as stdout, \
                (log_dir / "runner.stderr.log").open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                command, cwd=BENCHMARK_DIR, env=tlmsp_environment(False),
                stdout=stdout, stderr=stderr, text=True, check=False)
        monitor.stop()
        monitor.write()
        if completed.returncode not in (0, 2):
            raise RuntimeError(
                f"benchmark runner exited with status {completed.returncode}; "
                f"see {log_dir / 'runner.stderr.log'}")

        summary, window = parse_raw_events(events_path)
        cpu_percent = monitor.percent_between(window["start"], window["done"])
        valid = point_is_valid(summary)
        print(
            f"  completed={summary['completed']} successes={summary['successes']} "
            f"missed={summary['missed']} valid={int(valid)}", flush=True)
        result = {
            "experiment": args.experiment,
            "profile": profile,
            "mode": mode,
            "rate": f"{rate:g}",
            "run": run_number,
            "duration_s": args.duration_seconds,
            "scheduled": summary["scheduled"],
            "launched": summary["launched"],
            "completed": summary["completed"],
            "successes": summary["successes"],
            "errors": summary["errors"],
            "missed": summary["missed"],
            "achieved_rps": summary["achieved_rps"],
            "max_in_flight": summary["max_in_flight"],
            "valid": 1 if valid else 0,
            "middlebox_tree_cpu_percent": (
                "" if cpu_percent is None else f"{cpu_percent:.6f}"),
            "cpu_scope": "tlmsp-mb plus reaped handler children; listener excluded",
            "samples_csv": str(samples_path.relative_to(output_dir)),
            "events_log": str(events_path.relative_to(output_dir)),
            "component_logs": str(log_dir.relative_to(output_dir)),
        }
    finally:
        stack.stop()
    if trace_application:
        application_trace = convert_application_trace(stack.appserver_trace)
        merge_application_trace(samples_path, application_trace)
    return result


def parse_list(text, transform):
    try:
        values = [transform(item.strip()) for item in text.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if not values:
        raise argparse.ArgumentTypeError("list cannot be empty")
    return values


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a local, self-contained TLMSP benchmark campaign")
    parser.add_argument("experiment", choices=("latency", "throughput", "handshake"))
    parser.add_argument("--output-dir", type=Path, default=default_output())
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--warmup-seconds", type=float, default=1.0)
    parser.add_argument("--post-warmup-pause-seconds", type=float, default=1.0)
    parser.add_argument("--persistent-prime-pause-seconds", type=float, default=1.0)
    parser.add_argument("--cpu-interval-seconds", type=float, default=0.05)
    parser.add_argument("--readiness-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--max-in-flight", type=int, default=1024)
    parser.add_argument("--fresh-rate", type=float, default=1.0)
    parser.add_argument("--persistent-rate", type=float, default=10.0)
    parser.add_argument(
        "--rates", type=lambda value: parse_list(value, float),
        default=None, help="comma-separated offered rates")
    parser.add_argument(
        "--profiles", type=lambda value: parse_list(value, str),
        default=None, help="comma-separated full,no_handler profiles")
    args = parser.parse_args()
    if args.rates is None:
        if args.experiment == "throughput":
            args.rates = [1, 2, 5, 10, 25, 40, 50, 60, 75, 100, 125, 150]
        elif args.experiment == "handshake":
            args.rates = [
                1, 2, 5, 10, 25, 50, 75, 100, 125, 150, 175, 200,
                250, 500,
            ]
        else:
            args.rates = []
    if args.profiles and any(
            profile not in ("full", "no_handler") for profile in args.profiles):
        parser.error("profiles must be full or no_handler")
    numeric = (
        args.runs, args.duration_seconds, args.cpu_interval_seconds,
        args.readiness_timeout_seconds, args.max_in_flight,
        args.fresh_rate, args.persistent_rate,
    )
    if any(value <= 0 for value in numeric) or any(rate <= 0 for rate in args.rates):
        parser.error("runs, durations, intervals, limits, and rates must be positive")
    if args.warmup_seconds < 0 or args.post_warmup_pause_seconds < 0 or \
            args.persistent_prime_pause_seconds < 0:
        parser.error("warmup and pause durations must be nonnegative")
    return args


def preflight():
    required = [APP_SERVER, LOADGEN, RUN_LATENCY, MIDDLEBOX_DIR / "listener",
                MIDDLEBOX_DIR / "client", TLMSP_PREFIX / "bin/httpd",
                TLMSP_PREFIX / "bin/tlmsp-mb", FULL_CONFIG, NO_HANDLER_CONFIG]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("missing required paths: " + ", ".join(missing))


def main():
    args = parse_args()
    preflight()
    args.output_dir = args.output_dir.expanduser().resolve()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise RuntimeError(
            f"output directory is not empty: {args.output_dir}; "
            "choose a new directory or move the existing campaign")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": sys.argv,
        "method": {
            "duration_seconds": args.duration_seconds,
            "isolated_warmup": True,
            "warmup_seconds": args.warmup_seconds,
            "post_warmup_pause_seconds": args.post_warmup_pause_seconds,
            "persistent_prime": True,
            "persistent_prime_pause_seconds": args.persistent_prime_pause_seconds,
            "open_loop": True,
            "missed_slots_are_not_replayed": True,
            "timestamp_clock": "CLOCK_REALTIME Unix nanoseconds",
            "application_trace": "Full latency profile only",
            "cpu_scope": "tlmsp-mb plus reaped handler children; listener excluded",
        },
    }
    (args.output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    summary_path = args.output_dir / "summary.csv"
    rows = []
    for run_number in range(1, args.runs + 1):
        for profile, mode, rate in planned_points(args):
            rows.append(run_point(
                args, profile, mode, rate, run_number, args.output_dir))
            with summary_path.open("w", newline="", encoding="utf-8") as target:
                writer = csv.DictWriter(target, fieldnames=SUMMARY_FIELDS)
                writer.writeheader()
                writer.writerows(rows)

    print(f"wrote campaign to {args.output_dir}")
    print(f"summary: {summary_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
