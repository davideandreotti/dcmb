#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import csv
from collections import Counter
import aiohttp
import shutil
import ssl
import statistics
import subprocess
import time
from pathlib import Path

# Array di rate (ms tra richieste)
RATES = [15, 12, 10, 7, 5, 3, 2, 1, 0.5]

# Finestra temporale (secondi) entro cui inviare tutte le richieste possibili per ogni rate
TIME_REQUESTS_SECONDS = 20

# Token richieste client
TOKEN = "token"

SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR / "PerformanceMeasuring").exists() and (SCRIPT_DIR / "DC/Middlebox").exists():
    PROJECT_ROOT = SCRIPT_DIR
elif (SCRIPT_DIR / "MasterThesis/PerformanceMeasuring").exists():
    PROJECT_ROOT = SCRIPT_DIR / "MasterThesis"
else:
    PROJECT_ROOT = SCRIPT_DIR

SERVER_DIR = PROJECT_ROOT / "PerformanceMeasuring"

EXTERNAL_CERTS_DIR = PROJECT_ROOT / "certs_external"
CLIENT_CA_HOST_PATH = EXTERNAL_CERTS_DIR / "ca.crt"

BASE_DIR = PROJECT_ROOT / "ThroughputClientServer"
RESULTS_DIR = BASE_DIR / "Risultati"
RUNTIME_DIR = BASE_DIR / "Runtime"
GRAFICI_DIR = BASE_DIR / "Grafici"

CLIENT_RUNTIME_LOG = RUNTIME_DIR / "Client.log"
SERVER_RUNTIME_LOG = RUNTIME_DIR / "Server.log"

SERVER_SCRIPT = SERVER_DIR / "certs_server.py"

STARTUP_TIMEOUT = 60
CLIENT_REQUEST_TIMEOUT_SECONDS = 120


def compute_requests_for_rate(rate_ms: float) -> int:
    if rate_ms <= 0:
        raise ValueError("rate_ms deve essere > 0")
    window_ms = int(TIME_REQUESTS_SECONDS * 1000)
    return max(1, int(window_ms // rate_ms))


############################################
# UTILITA GENERALI
############################################

def set_cpu_performance_governor() -> None:
    """Imposta il CPU governor a 'performance' per eliminare il cold-start da frequency scaling."""
    # Prova prima via sysfs (no dipendenze esterne)
    gov_paths = list(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor"))
    if gov_paths:
        try:
            result = subprocess.run(
                ["sudo", "-n", "tee"] + [str(p) for p in gov_paths],
                input="performance", capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                print(f"[SETUP] CPU governor impostato a 'performance' su {len(gov_paths)} core")
                return
        except subprocess.TimeoutExpired:
            pass

    # Fallback: cpupower
    if shutil.which("cpupower") is not None:
        try:
            result = subprocess.run(
                ["sudo", "-n", "cpupower", "frequency-set", "-g", "performance"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                print("[SETUP] CPU governor impostato a 'performance' via cpupower")
                return
        except subprocess.TimeoutExpired:
            pass

    print("[SETUP] Impossibile impostare CPU governor automaticamente.")
    print("        Per risultati flat, esegui prima del test:")
    print("        sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $f; done'")


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
    for path in (CLIENT_RUNTIME_LOG, SERVER_RUNTIME_LOG):
        if path.exists():
            path.unlink()





def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def start_server_process() -> subprocess.Popen[str]:
    reset_runtime_logs()
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
    start_index = 0
    while time.time() < deadline:
        content = read_text(SERVER_RUNTIME_LOG)
        if marker in content[start_index:]:
            server_log.close()
            return process
        if process.poll() is not None:
            server_log.close()
            raise RuntimeError(f"Il server locale è terminato inaspettatamente. Log:\n{content[-2000:]}")
        time.sleep(0.1)

    server_log.close()
    process.terminate()
    raise TimeoutError(f"Marker non trovato nel log server: {marker}")


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


############################################
# THROUGHPUT TEST
############################################

def _build_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(CLIENT_CA_HOST_PATH))
    context.check_hostname = False
    return context


def run_throughput_test(rate_ms: float) -> dict:
    reset_runtime_logs()
    total_requests = compute_requests_for_rate(rate_ms)

    latencies_by_req: list[int | None] = [None] * total_requests
    t1_by_req_ns: list[int | None] = [None] * total_requests
    t10_by_req_ns: list[int | None] = [None] * total_requests
    dispatch_delay_by_req_ms: list[float | None] = [None] * total_requests
    status_by_req: list[str] = ["pending"] * total_requests
    error_reason_by_req: list[str | None] = [None] * total_requests

    request_timeout = CLIENT_REQUEST_TIMEOUT_SECONDS

    interval_ns = int(rate_ms * 1_000_000)
    schedule_start_ns = time.perf_counter_ns()
    ssl_context = _build_ssl_context()

    request_url = "https://127.0.0.1:8000/function/init"

    async def send_one(req_index: int, send_ts_ns: int, session: aiohttp.ClientSession) -> None:
        try:
            async with session.post(request_url, headers={"Authorization": f"Bearer {TOKEN}"}) as response:
                raw_response = await response.read()
                status_code = response.status

            t10_ns = time.perf_counter_ns()
            t10_by_req_ns[req_index] = t10_ns

            response_text = raw_response.decode("utf-8", errors="replace")
            if response_text:
                append_text(CLIENT_RUNTIME_LOG, response_text)

            if 200 <= status_code < 300:
                delta = t10_ns - send_ts_ns
                if delta > 0:
                    latencies_by_req[req_index] = delta
                    status_by_req[req_index] = "ok"
                else:
                    status_by_req[req_index] = "invalid_latency"
            else:
                status_by_req[req_index] = "client_error"

        except (asyncio.TimeoutError, TimeoutError):
            status_by_req[req_index] = "timeout"
        except aiohttp.ClientError as exc:
            status_by_req[req_index] = "error"
            error_reason_by_req[req_index] = type(exc).__name__
        except Exception as exc:
            status_by_req[req_index] = "error"
            error_reason_by_req[req_index] = type(exc).__name__

    async def run_open_loop() -> None:
        tasks: list[asyncio.Task[None]] = []
        semaphore = asyncio.Semaphore(200)

        async def send_one_limited(req_index: int, send_ts_ns: int, session: aiohttp.ClientSession) -> None:
            async with semaphore:
                await send_one(req_index, send_ts_ns, session)

        timeout = aiohttp.ClientTimeout(total=request_timeout)
        connector = aiohttp.TCPConnector(
            ssl=ssl_context,
            force_close=True,
        )

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            for req_index in range(total_requests):
                planned_ns = schedule_start_ns + req_index * interval_ns
                now_ns = time.perf_counter_ns()
                await asyncio.sleep(max(0.0, (planned_ns - now_ns) / 1_000_000_000.0))

                send_ts_ns = time.perf_counter_ns()
                dispatch_delay_by_req_ms[req_index] = max(0.0, (send_ts_ns - planned_ns) / 1_000_000.0)
                t1_by_req_ns[req_index] = send_ts_ns

                tasks.append(asyncio.create_task(send_one_limited(req_index, send_ts_ns, session)))

            await asyncio.gather(*tasks)

    asyncio.run(run_open_loop())

    latencies = [value for value in latencies_by_req if value is not None]
    dispatch_delays_ok = [
        dispatch_delay_by_req_ms[i]
        for i in range(total_requests)
        if status_by_req[i] == "ok" and dispatch_delay_by_req_ms[i] is not None
    ]

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
        if missing_other > 0:
            reasons = Counter(reason for reason in error_reason_by_req if reason)
            if reasons:
                details = ", ".join(f"{name}={count}" for name, count in reasons.most_common(5))
                print(f"  Missing-other dettagli eccezioni: {details}")

    return {
        "rate_ms": rate_ms,
        "latencies": latencies,
        "dispatch_delays_ms": dispatch_delays_ok,
        "status_by_req": status_by_req,
        "service_latency_by_req_ns": latencies_by_req,
        "t1_by_req_ns": t1_by_req_ns,
        "t10_by_req_ns": t10_by_req_ns,
        "dispatch_delay_by_req_ms": dispatch_delay_by_req_ms,
        "total_requests": total_requests,
        "missing": missing,
        "missing_timeout": missing_timeout,
        "missing_client_error": missing_client_error,
        "missing_no_timestamp": missing_no_timestamp,
        "missing_other": missing_other,
    }


def compute_stats(latencies: list[int]) -> dict:
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


def run_throughput_suite() -> None:
    print("\n===== THROUGHPUT CLIENT -> SERVER =====")

    results = []
    raw_rows = []

    for rate_ms in RATES:
        print(f"\n--- Rate {rate_ms}ms ({1000/rate_ms:.2f} Hz) ---")
        planned_requests = compute_requests_for_rate(rate_ms)
        print(
            f"  Finestra {TIME_REQUESTS_SECONDS}s -> richieste pianificate: {planned_requests}"
        )

        test_result = run_throughput_test(rate_ms)
        latencies = test_result["latencies"]
        dispatch_delays_ms = test_result.get("dispatch_delays_ms", [])
        total_requests = test_result.get("total_requests", planned_requests)
        status_by_req = test_result.get("status_by_req", ["pending"] * total_requests)
        service_latency_by_req_ns = test_result.get("service_latency_by_req_ns", [None] * total_requests)
        dispatch_delay_by_req_ms = test_result.get("dispatch_delay_by_req_ms", [None] * total_requests)
        t1_by_req_ns = test_result.get("t1_by_req_ns", [None] * total_requests)
        t10_by_req_ns = test_result.get("t10_by_req_ns", [None] * total_requests)
        missing = test_result.get("missing", 0)
        missing_timeout = test_result.get("missing_timeout", 0)
        missing_client_error = test_result.get("missing_client_error", 0)
        missing_no_timestamp = test_result.get("missing_no_timestamp", 0)
        missing_other = test_result.get("missing_other", 0)

        stats = compute_stats(latencies)
        dispatch_stats = compute_stats([int(v * 1_000_000) for v in dispatch_delays_ms])

        result_row = {
            "rate_ms": rate_ms,
            "frequency_hz": round(1000 / rate_ms, 2),
            "num_requests": total_requests,
            "num_requests_ok": stats["count"],
            "missing_requests": missing,
            "missing_timeout": missing_timeout,
            "missing_client_error": missing_client_error,
            "missing_no_timestamp": missing_no_timestamp,
            "missing_other": missing_other,
            "mean_latency_ms": round(stats["mean_ms"], 3),
            "median_latency_ms": round(stats["median_ms"], 3),
            "std_latency_ms": round(stats["std_ms"], 3),
            "min_latency_ms": round(stats["min_ms"], 3),
            "max_latency_ms": round(stats["max_ms"], 3),
            "mean_dispatch_delay_ms": round(dispatch_stats["mean_ms"], 3),
            "median_dispatch_delay_ms": round(dispatch_stats["median_ms"], 3),
            # Coerenza con gli altri throughput script: nessuna coda lato middlebox.
            "mean_queue_delay_ms": 0.0,
            "median_queue_delay_ms": 0.0,
            "mean_total_latency_ms": round(stats["mean_ms"], 3),
            "median_total_latency_ms": round(stats["median_ms"], 3),
        }
        results.append(result_row)

        for req_idx in range(1, total_requests + 1):
            svc_ns = service_latency_by_req_ns[req_idx - 1] if req_idx - 1 < len(service_latency_by_req_ns) else None
            t1_ns = t1_by_req_ns[req_idx - 1] if req_idx - 1 < len(t1_by_req_ns) else None
            t10_ns = t10_by_req_ns[req_idx - 1] if req_idx - 1 < len(t10_by_req_ns) else None
            d_ms = dispatch_delay_by_req_ms[req_idx - 1] if req_idx - 1 < len(dispatch_delay_by_req_ms) else None
            status = status_by_req[req_idx - 1] if req_idx - 1 < len(status_by_req) else "pending"
            raw_rows.append(
                {
                    "rate_ms": rate_ms,
                    "request_index": req_idx,
                    "status": status,
                    "t1_ns": t1_ns if t1_ns is not None else "",
                    "t10_ns": t10_ns if t10_ns is not None else "",
                    "service_latency_ms": round(svc_ns / 1_000_000, 6) if svc_ns is not None else "",
                    "dispatch_delay_ms": round(d_ms, 6) if d_ms is not None else "",
                    "queue_delay_ms": 0.0 if status == "ok" else "",
                }
            )

        print(f"  Mean service latency: {result_row['mean_latency_ms']} ms")
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

    csv_path = RESULTS_DIR / "Throughput_ClientServer.csv"
    if results:
        with open(csv_path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"\n✓ Risultati salvati: {csv_path}")

    raw_csv_path = RESULTS_DIR / "Throughput_ClientServer_raw.csv"
    if raw_rows:
        with open(raw_csv_path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=raw_rows[0].keys())
            writer.writeheader()
            writer.writerows(raw_rows)
        print(f"✓ Latenze raw salvate: {raw_csv_path}")


############################################
# MAIN
############################################

def main() -> None:
    server_process: subprocess.Popen[str] | None = None
    try:
        ensure_directories()
        cleanup_old_output_files()
        set_cpu_performance_governor()

        print("\n[SETUP] Processi client-server locali...")
        server_process = start_server_process()

        try:
            run_throughput_suite()
        finally:
            stop_server_process(server_process)

        print("\n✓ Test throughput client-server completato")

    except Exception as exc:
        print(f"ERRORE durante main: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            stop_server_process(server_process)
        except Exception:
            pass


if __name__ == "__main__":
    main()
