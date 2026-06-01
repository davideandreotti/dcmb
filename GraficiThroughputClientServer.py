from pathlib import Path
import csv
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np


############################################
# CONFIGURAZIONE
############################################

BASE_DIR = Path(__file__).resolve().parent / "ThroughputClientServer"
RESULTS_DIR = BASE_DIR / "Risultati"
GRAFICI_DIR = BASE_DIR / "Grafici"
MISURE_RESULTS_PATH = Path(__file__).resolve().parent / "Misure" / "Analisi" / "Risultati.txt"

GRAFICI_DIR.mkdir(parents=True, exist_ok=True)


############################################
# LETTURA DATI
############################################

def read_results_csv() -> dict | None:
    csv_path = RESULTS_DIR / "Throughput_ClientServer.csv"
    if not csv_path.exists():
        print(f"Avviso: {csv_path} non trovato")
        return None

    data = {
        "rate_ms": [],
        "frequency_hz": [],
        "num_requests": [],
        "num_requests_ok": [],
        "mean_latency_ms": [],
        "median_latency_ms": [],
        "std_latency_ms": [],
        "min_latency_ms": [],
        "max_latency_ms": [],
        "mean_dispatch_delay_ms": [],
        "median_dispatch_delay_ms": [],
        "mean_queue_delay_ms": [],
        "median_queue_delay_ms": [],
        "mean_total_latency_ms": [],
        "median_total_latency_ms": [],
        "missing_requests": [],
        "missing_timeout": [],
        "missing_client_error": [],
        "missing_no_timestamp": [],
        "missing_other": [],
    }

    with open(csv_path, "r") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            data["rate_ms"].append(float(row["rate_ms"]))
            data["frequency_hz"].append(float(row["frequency_hz"]))
            data["num_requests"].append(int(row.get("num_requests", "0") or "0"))
            data["num_requests_ok"].append(int(row.get("num_requests_ok", "0") or "0"))
            data["mean_latency_ms"].append(float(row["mean_latency_ms"]))
            data["median_latency_ms"].append(float(row["median_latency_ms"]))
            data["std_latency_ms"].append(float(row["std_latency_ms"]))
            data["min_latency_ms"].append(float(row["min_latency_ms"]))
            data["max_latency_ms"].append(float(row["max_latency_ms"]))
            data["mean_dispatch_delay_ms"].append(float(row.get("mean_dispatch_delay_ms", "0") or "0"))
            data["median_dispatch_delay_ms"].append(float(row.get("median_dispatch_delay_ms", "0") or "0"))
            data["mean_queue_delay_ms"].append(float(row.get("mean_queue_delay_ms", "0") or "0"))
            data["median_queue_delay_ms"].append(float(row.get("median_queue_delay_ms", "0") or "0"))
            data["mean_total_latency_ms"].append(float(row.get("mean_total_latency_ms", "0") or "0"))
            data["median_total_latency_ms"].append(float(row.get("median_total_latency_ms", "0") or "0"))
            data["missing_requests"].append(int(row.get("missing_requests", "0") or "0"))
            data["missing_timeout"].append(int(row.get("missing_timeout", "0") or "0"))
            data["missing_client_error"].append(int(row.get("missing_client_error", "0") or "0"))
            data["missing_no_timestamp"].append(int(row.get("missing_no_timestamp", "0") or "0"))
            data["missing_other"].append(int(row.get("missing_other", "0") or "0"))

    return data


def read_raw_results_csv() -> dict[float, dict[str, list[float]]]:
    csv_path = RESULTS_DIR / "Throughput_ClientServer_raw.csv"
    if not csv_path.exists():
        return {}

    raw_by_rate: dict[float, dict[str, list[float]]] = {}
    with open(csv_path, "r") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rate = float(row["rate_ms"])
            bucket = raw_by_rate.setdefault(rate, {"service": [], "dispatch": [], "total": [], "t1_ns": [], "t10_ns": []})

            service_raw = row.get("service_latency_ms", "")
            dispatch_raw = row.get("dispatch_delay_ms", "")
            t1_raw = row.get("t1_ns", "")
            t10_raw = row.get("t10_ns", "")

            service_ms = None
            dispatch_ms = 0.0

            if service_raw:
                service_ms = float(service_raw)
                bucket["service"].append(service_ms)

            if dispatch_raw:
                dispatch_ms = float(dispatch_raw)
                bucket["dispatch"].append(dispatch_ms)

            if service_ms is not None:
                bucket["total"].append(service_ms)

            if t1_raw:
                bucket["t1_ns"].append(float(t1_raw))
            if t10_raw:
                bucket["t10_ns"].append(float(t10_raw))

    return raw_by_rate


def read_op4_baseline_t10_t1_ms() -> float | None:
    if not MISURE_RESULTS_PATH.exists():
        return None

    text = MISURE_RESULTS_PATH.read_text(encoding="utf-8", errors="replace")
    marker = "AVERAGE operazione_N_4:"
    marker_pos = text.find(marker)
    if marker_pos == -1:
        return None

    tail = text[marker_pos:]
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("--------------------"):
            break
        if stripped.startswith("t10 - t1 average =") and stripped.endswith("ns"):
            try:
                value_ns = float(stripped.split("=")[1].replace("ns", "").strip())
                return value_ns / 1_000_000.0
            except Exception:
                return None
    return None


############################################
# GRAFICI PER-RATE
############################################

def plot_per_rate_graphs() -> None:
    results = read_results_csv()
    if not results:
        return

    raw_by_rate = read_raw_results_csv()
    baseline_op4_ms = read_op4_baseline_t10_t1_ms()

    for i, rate_ms in enumerate(results["rate_ms"]):
        fig, ax1 = plt.subplots(1, 1, figsize=(9, 5))

        mean_service = results["mean_latency_ms"][i]
        mean_total = results["mean_total_latency_ms"][i]
        median_total = results["median_total_latency_ms"][i]

        bucket = raw_by_rate.get(rate_ms, {"service": [], "dispatch": [], "total": [], "t1_ns": [], "t10_ns": []})
        latencies_total = bucket["total"]
        if not latencies_total:
            latencies_total = [mean_total]

        y_max = max(latencies_total) * 1.15 if latencies_total else mean_total * 1.5

        x_scatter = np.arange(1, len(latencies_total) + 1)

        ax1.scatter(
            x_scatter,
            latencies_total,
            alpha=0.65,
            s=30,
            color="steelblue",
            label="Latenza totale per richiesta",
        )
        ax1.axhline(y=mean_total, color="navy", linestyle="--", linewidth=2, label=f"Media totale: {mean_total:.2f}ms")
        ax1.axhline(y=median_total, color="cornflowerblue", linestyle=":", linewidth=2, label=f"Mediana totale: {median_total:.2f}ms")
        ax1.axhline(y=mean_service, color="teal", linestyle="-.", linewidth=2, label=f"Media servizio (t10-t1): {mean_service:.2f}ms")
        if baseline_op4_ms is not None:
            ax1.axhline(
                y=baseline_op4_ms,
                color="darkred",
                linestyle="-",
                linewidth=1.8,
                label=f"Riferimento Misure.py op4 (t10-t1): {baseline_op4_ms:.2f}ms",
            )
        ax1.set_xlabel("Numero richiesta", fontsize=11)
        ax1.set_ylabel("Latenza totale (ms)", fontsize=11)
        ax1.set_title(f"Scatter Plot Totale - Rate {rate_ms}ms ({1000/rate_ms:.2f} Hz)", fontsize=12, fontweight="bold")
        ax1.legend(fontsize=10)
        ax1.grid(True, alpha=0.3)
        ax1.set_ylim(bottom=0, top=y_max)

        plt.tight_layout()
        png_path = GRAFICI_DIR / f"Throughput_ClientServer_Rate_{int(rate_ms)}ms.png"
        plt.savefig(png_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  ✓ {png_path.name}")


############################################
# GRAFICO CUMULATIVO
############################################

def plot_cumulative_graph() -> None:
    results = read_results_csv()
    if not results:
        return

    y_max = max(results["mean_latency_ms"]) * 1.1 if results["mean_latency_ms"] else 100
    baseline_op4_ms = read_op4_baseline_t10_t1_ms()

    pairs = sorted(zip(results["rate_ms"], results["mean_latency_ms"]), reverse=True)
    rate_ms_sorted = [p[0] for p in pairs]
    latencies_sorted = [p[1] for p in pairs]
    x_pos = list(range(len(rate_ms_sorted)))
    x_labels = [f"1/{int(r)}" for r in rate_ms_sorted]

    def _draw(ax) -> None:
        ax.plot(
            x_pos,
            latencies_sorted,
            marker="o",
            linewidth=2.5,
            markersize=8,
            label="Client-Server diretto",
            color="steelblue",
            alpha=0.85,
        )
        ax.scatter(x_pos, latencies_sorted, s=120, alpha=0.6, color="steelblue")
        ax.set_xticks(x_pos)
        ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=10)
        ax.set_xlabel("Frequenza (1/rate_ms)", fontsize=12, fontweight="bold")
        ax.set_ylabel("Latenza Media Servizio (ms)", fontsize=12, fontweight="bold")
        ax.set_title(
            "Throughput Client-Server: Latenza Servizio (t10-t1) vs Frequenza Richieste",
            fontsize=14,
            fontweight="bold",
        )
        ax.legend(fontsize=11, loc="best")
        ax.grid(True, alpha=0.3)
        if baseline_op4_ms is not None:
            ax.axhline(
                y=baseline_op4_ms,
                color="darkred",
                linestyle="-",
                linewidth=2,
                label=f"Riferimento Misure.py op4 (t10-t1): {baseline_op4_ms:.2f}ms",
            )
            ax.legend(fontsize=11, loc="best")
        ax.set_ylim(bottom=0, top=y_max)

    fig, ax = plt.subplots(figsize=(12, 7))
    _draw(ax)
    plt.tight_layout()
    png_path = GRAFICI_DIR / "Throughput_ClientServer_Cumulativo.png"
    plt.savefig(png_path, dpi=150, bbox_inches="tight")
    print(f"  ✓ {png_path.name}")
    plt.close()

    with PdfPages(GRAFICI_DIR / "Throughput_ClientServer_Cumulativo.pdf") as pdf:
        fig, ax = plt.subplots(figsize=(12, 7))
        _draw(ax)
        plt.tight_layout()
        pdf.savefig(fig, bbox_inches="tight")
        plt.close()
    print(f"  ✓ {GRAFICI_DIR / 'Throughput_ClientServer_Cumulativo.pdf'}")


############################################
# TABELLA RIEPILOGATIVA
############################################

def create_summary_table() -> None:
    results = read_results_csv()
    if not results:
        return

    summary_rows = []
    for i, rate_ms in enumerate(results["rate_ms"]):
        summary_rows.append(
            {
                "Rate (ms)": rate_ms,
                "Frequenza (Hz)": results["frequency_hz"][i],
                "Richieste Totali": results["num_requests"][i],
                "Richieste OK": results["num_requests_ok"][i],
                "Missing": results["missing_requests"][i],
                "Servizio Medio (ms)": results["mean_latency_ms"][i],
                "Ritardo Invio Medio (ms)": results["mean_dispatch_delay_ms"][i],
                "Servizio Mediana (ms)": results["median_latency_ms"][i],
                "Std Dev (ms)": results["std_latency_ms"][i],
                "Min (ms)": results["min_latency_ms"][i],
                "Max (ms)": results["max_latency_ms"][i],
                "Missing Timeout": results["missing_timeout"][i],
                "Missing Client Error": results["missing_client_error"][i],
                "Missing No Timestamp": results["missing_no_timestamp"][i],
                "Missing Other": results["missing_other"][i],
            }
        )

    if summary_rows:
        summary_path = GRAFICI_DIR / "Throughput_ClientServer_Summary.csv"
        with open(summary_path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=summary_rows[0].keys())
            writer.writeheader()
            writer.writerows(summary_rows)
        print(f"  ✓ {summary_path.name}")


############################################
# MAIN
############################################

def main() -> None:
    print("[GRAFICI] Generazione grafici throughput Client-Server...\n")

    results_file = RESULTS_DIR / "Throughput_ClientServer.csv"
    if not results_file.exists():
        print("Errore: file risultati non trovato in Risultati/")
        print("Esegui ThroughputClientServer.py prima di generare grafici.")
        return

    print("[PER-RATE GRAPHS]")
    plot_per_rate_graphs()

    print("\n[CUMULATIVE GRAPH]")
    plot_cumulative_graph()

    print("\n[SUMMARY TABLE]")
    create_summary_table()

    print(f"\n✓ Grafici salvati in: {GRAFICI_DIR}")
    print(f"   Visualizza: {GRAFICI_DIR / 'Throughput_ClientServer_Cumulativo.png'}")


if __name__ == "__main__":
    main()
