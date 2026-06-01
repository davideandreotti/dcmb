from pathlib import Path
import csv
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np


############################################
# CONFIGURAZIONE
############################################

BASE_DIR = Path(__file__).resolve().parent / "ThroughputDocker"
RESULTS_DIR = BASE_DIR / "Risultati"
GRAFICI_DIR = BASE_DIR / "Grafici"

GRAFICI_DIR.mkdir(parents=True, exist_ok=True)


############################################
# LETTURA DATI
############################################

def read_results_csv(scenario: int) -> dict:
    """
    Legge CSV di risultati per scenario.
    Ritorna: {"rate_ms": [...], "frequency_hz": [...], "mean_latency_ms": [...]}
    """
    csv_path = RESULTS_DIR / f"Throughput_Scenario_{scenario}.csv"
    
    if not csv_path.exists():
        print(f"Avviso: {csv_path} non trovato")
        return None
    
    data = {
        "rate_ms": [],
        "frequency_hz": [],
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
    }
    
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mean_latency = float(row["mean_latency_ms"])
            median_latency = float(row["median_latency_ms"])
            data["rate_ms"].append(float(row["rate_ms"]))
            data["frequency_hz"].append(float(row["frequency_hz"]))
            data["mean_latency_ms"].append(mean_latency)
            data["median_latency_ms"].append(median_latency)
            data["std_latency_ms"].append(float(row["std_latency_ms"]))
            data["min_latency_ms"].append(float(row["min_latency_ms"]))
            data["max_latency_ms"].append(float(row["max_latency_ms"]))
            mean_dispatch = float(row.get("mean_dispatch_delay_ms", "0") or "0")
            median_dispatch = float(row.get("median_dispatch_delay_ms", "0") or "0")
            mean_queue = float(row.get("mean_queue_delay_ms", "0") or "0")
            median_queue = float(row.get("median_queue_delay_ms", "0") or "0")
            mean_total = float(row.get("mean_total_latency_ms", "0") or "0")
            median_total = float(row.get("median_total_latency_ms", "0") or "0")
            data["mean_dispatch_delay_ms"].append(mean_dispatch)
            data["median_dispatch_delay_ms"].append(median_dispatch)
            data["mean_queue_delay_ms"].append(mean_queue)
            data["median_queue_delay_ms"].append(median_queue)
            data["mean_total_latency_ms"].append(mean_total)
            data["median_total_latency_ms"].append(median_total)
    
    return data


def read_raw_results_csv(scenario: int) -> dict[float, dict[str, list[float]]]:
    """
    Legge i campioni raw per scenario.
    Ritorna: {rate_ms: {"service": [...], "queue": [...], "total": [...]}}
    Calcola total = service + queue per ogni richiesta.
    """
    csv_path = RESULTS_DIR / f"Throughput_Scenario_{scenario}_raw.csv"
    if not csv_path.exists():
        return {}

    raw_by_rate: dict[float, dict[str, list[float]]] = {}
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rate = float(row["rate_ms"])
            bucket = raw_by_rate.setdefault(rate, {"service": [], "queue": [], "total": []})

            service_raw = row.get("service_latency_ms", "")
            queue_raw = row.get("queue_delay_ms", "")

            service_ms = None
            queue_ms = None
            
            if service_raw:
                service_ms = float(service_raw)
                bucket["service"].append(service_ms)
            if queue_raw:
                queue_ms = float(queue_raw)
                bucket["queue"].append(queue_ms)
            
            # Calcola totale = service + queue
            if service_ms is not None and queue_ms is not None:
                bucket["total"].append(service_ms + queue_ms)
            elif service_ms is not None:
                bucket["total"].append(service_ms)
    return raw_by_rate


############################################
# GRAFICI PER-RATE
############################################

def plot_per_rate_graphs(scenario: int) -> None:
    """
    Genera PNG per-rate: scatter plot (latenze individuali) + violin plot (distribuzione).
    """
    results = read_results_csv(scenario)
    if not results:
        return
    
    raw_by_rate = read_raw_results_csv(scenario)
    
    for i, rate_ms in enumerate(results["rate_ms"]):
        fig, ax1 = plt.subplots(1, 1, figsize=(9, 5))
        
        # Dati statistici per questo rate
        mean_service = results["mean_latency_ms"][i]
        mean_total = results["mean_total_latency_ms"][i]
        median_total = results["median_total_latency_ms"][i]

        bucket = raw_by_rate.get(rate_ms, {"service": [], "queue": [], "total": []})
        latencies_total = bucket["total"]
        if not latencies_total:
            latencies_total = [mean_total]
        x_scatter = np.arange(1, len(latencies_total) + 1)

        y_max = max(latencies_total) * 1.15 if latencies_total else mean_total * 1.5
        
        ax1.scatter(x_scatter, latencies_total, alpha=0.65, s=30, color='steelblue', label='Latenza totale per richiesta')
        ax1.axhline(y=mean_total, color='navy', linestyle='--', linewidth=2, label=f'Media totale: {mean_total:.2f}ms')
        ax1.axhline(y=median_total, color='cornflowerblue', linestyle=':', linewidth=2, label=f'Mediana totale: {median_total:.2f}ms')
        ax1.axhline(y=mean_service, color='teal', linestyle='-.', linewidth=2, label=f'Media servizio (t10-t1): {mean_service:.2f}ms')
        ax1.set_xlabel('Numero richiesta', fontsize=11)
        ax1.set_ylabel('Latenza totale (ms)', fontsize=11)
        ax1.set_title(f'Scatter Plot Totale - Rate {rate_ms}ms ({1000/rate_ms:.2f} Hz)', fontsize=12, fontweight='bold')
        ax1.legend(fontsize=10)
        ax1.grid(True, alpha=0.3)
        ax1.set_ylim(bottom=0, top=y_max)
        
        plt.tight_layout()
        png_path = GRAFICI_DIR / f"Throughput_Scenario_{scenario}_Rate_{int(rate_ms)}ms.png"
        plt.savefig(png_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"  ✓ {png_path.name}")


############################################
# GRAFICO CUMULATIVO
############################################

def plot_cumulative_graph(scenarios: list[int] = [1, 2, 3]) -> None:
    """
    Grafico cumulativo: ascisse = 1/rate (in ordine crescente), ordinate = latenza media totale.
    Una curva per scenario.
    """
    colors = {1: 'steelblue', 2: 'royalblue', 3: 'deepskyblue'}
    labels = {
        1: 'Scenario 1: Delega Reset',
        2: 'Scenario 2: Delega Cache',
        3: 'Scenario 3: Orchestrazione Swarm'
    }
    markers = {1: 'o', 2: 's', 3: '^'}
    
    # Pre-carica tutti i dati disponibili e calcola y_max globale
    scenario_data = {}
    all_means = []
    for sc in scenarios:
        res = read_results_csv(sc)
        if res:
            scenario_data[sc] = res
            all_means.extend(res["mean_latency_ms"])
        else:
            print(f"  Scenario {sc}: CSV non trovato, skipping...")
    
    y_max = max(all_means) * 1.1 if all_means else 100
    
    def _draw(ax) -> None:
        for scenario, results in scenario_data.items():
            pairs = sorted(zip(results["rate_ms"], results["mean_latency_ms"]), reverse=True)
            rate_ms_sorted = [p[0] for p in pairs]
            latencies_sorted = [p[1] for p in pairs]
            x_pos = list(range(len(rate_ms_sorted)))
            x_labels = [f'1/{int(r)}' for r in rate_ms_sorted]
            
            ax.plot(x_pos, latencies_sorted,
                    marker=markers[scenario], linewidth=2.5, markersize=8,
                    label=labels[scenario], color=colors[scenario], alpha=0.85)
            ax.scatter(x_pos, latencies_sorted, s=120, alpha=0.6, color=colors[scenario])
            ax.set_xticks(x_pos)
            ax.set_xticklabels(x_labels, rotation=45, ha='right', fontsize=10)
        
        ax.set_xlabel('Frequenza (1/rate_ms)', fontsize=12, fontweight='bold')
        ax.set_ylabel('Latenza Media Servizio (ms)', fontsize=12, fontweight='bold')
        ax.set_title('Throughput Analysis: Latenza Servizio (t10-t1) vs Frequenza Richieste', fontsize=14, fontweight='bold')
        ax.legend(fontsize=11, loc='best')
        ax.grid(True, alpha=0.3)
        ax.set_ylim(bottom=0, top=y_max)
    
    # Esporta PNG
    fig, ax = plt.subplots(figsize=(12, 7))
    _draw(ax)
    plt.tight_layout()
    png_path = GRAFICI_DIR / "Throughput_Cumulativo.png"
    plt.savefig(png_path, dpi=150, bbox_inches='tight')
    print(f"  ✓ {png_path.name}")
    plt.close()
    
    # Esporta PDF
    with PdfPages(GRAFICI_DIR / "Throughput_Cumulativo.pdf") as pdf:
        fig, ax = plt.subplots(figsize=(12, 7))
        _draw(ax)
        plt.tight_layout()
        pdf.savefig(fig, bbox_inches='tight')
        plt.close()
    print(f"  ✓ {GRAFICI_DIR / 'Throughput_Cumulativo.pdf'}")


def plot_delay_breakdown_all_scenarios() -> None:
    """
    Genera breakdown per TUTTI gli scenari: separa servizio (t10-t1, costante ~11ms)
    e coda nel middlebox (queue_delay, cresce a bassi rate).
    Stack plot: blu (servizio) + arancio (coda) = totale.
    Salva in PDF e PNG.
    """
    # Baseline dei servizi da Misure.py (t10-t1 medio per operazione)
    baseline_ms = {
        1: 10.72,  # Operazione 2: t10-t1 = 10719910 ns
        2: 3.64,   # Operazione 3: t10-t1 = 3639570 ns
        3: 11.0,   # Scenario 3: baseline stimato
    }
    
    for scenario in [1, 2, 3]:
        results = read_results_csv(scenario)
        if not results:
            print(f"  Scenario {scenario}: CSV non trovato, skip breakdown.")
            continue

        # Ordina per rate decrescente
        pairs = sorted(
            zip(
                results["rate_ms"],
                results["mean_latency_ms"],
                results["mean_queue_delay_ms"],
            ),
            reverse=True,
        )
        rates = [p[0] for p in pairs]
        service_vals = [p[1] for p in pairs]
        queue_vals = [p[2] for p in pairs]

        x = np.arange(len(rates))
        labels = [f"{int(r)}ms" for r in rates]
        
        baseline = baseline_ms.get(scenario, 11.0)

        # Crea figura
        fig, ax = plt.subplots(figsize=(14, 8))
        
        # Stack plot: servizio (blu) + coda (arancio)
        ax.bar(x, service_vals, label='Servizio (t10-t1)', color='steelblue', alpha=0.85, width=0.6)
        ax.bar(x, queue_vals, bottom=service_vals, label='Coda nel Middlebox', color='orange', alpha=0.75, width=0.6)
        
        # Linea baseline del servizio da Misure.py
        ax.axhline(y=baseline, color='teal', linestyle=':', linewidth=2, label=f'Baseline Servizio {baseline:.2f}ms', alpha=0.7)
        
        # Etichette e formatting
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=10)
        ax.set_xlabel('Rate (ms)', fontsize=12, fontweight='bold')
        ax.set_ylabel('Tempo (ms)', fontsize=12, fontweight='bold')
        
        scenario_label = {
            1: "Scenario 1 - Delega Reset/Reauth",
            2: "Scenario 2 - Delega Cache",
            3: "Scenario 3 - Orchestrazione Swarm"
        }[scenario]
        ax.set_title(f'Breakdown Latenza: {scenario_label}', fontsize=14, fontweight='bold')
        
        ax.grid(True, alpha=0.3, axis='y')
        ax.set_ylim(bottom=0)
        ax.legend(loc='upper left', fontsize=11)
        
        plt.tight_layout()
        
        # Esporta PNG
        png_path = GRAFICI_DIR / f"Scenario{scenario}_Breakdown_Ritardo.png"
        plt.savefig(png_path, dpi=150, bbox_inches='tight')
        print(f"  ✓ {png_path.name}")
        plt.close()
        
        # Esporta PDF
        pdf_path = GRAFICI_DIR / f"Scenario{scenario}_Breakdown_Ritardo.pdf"
        with PdfPages(pdf_path) as pdf:
            fig, ax = plt.subplots(figsize=(14, 8))
            ax.bar(x, service_vals, label='Servizio (t10-t1)', color='steelblue', alpha=0.85, width=0.6)
            ax.bar(x, queue_vals, bottom=service_vals, label='Coda nel Middlebox', color='orange', alpha=0.75, width=0.6)
            ax.axhline(y=baseline, color='teal', linestyle=':', linewidth=2, label=f'Baseline Servizio {baseline:.2f}ms', alpha=0.7)
            ax.set_xticks(x)
            ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=10)
            ax.set_xlabel('Rate (ms)', fontsize=12, fontweight='bold')
            ax.set_ylabel('Tempo (ms)', fontsize=12, fontweight='bold')
            ax.set_title(f'Breakdown Latenza: {scenario_label}', fontsize=14, fontweight='bold')
            ax.grid(True, alpha=0.3, axis='y')
            ax.set_ylim(bottom=0)
            ax.legend(loc='upper left', fontsize=11)
            plt.tight_layout()
            pdf.savefig(fig, bbox_inches='tight')
            plt.close()
        print(f"  ✓ {pdf_path.name}")


def create_summary_table() -> None:
    """
    Crea una tabella CSV riepilogativa con i dati aggregati di tutti gli scenari.
    """
    summary_rows = []
    
    for scenario in [1, 2, 3]:
        results = read_results_csv(scenario)
        if not results:
            continue
        
        for i, rate_ms in enumerate(results["rate_ms"]):
            summary_rows.append({
                "Scenario": scenario,
                "Rate (ms)": rate_ms,
                "Frequenza (Hz)": results["frequency_hz"][i],
                "Servizio Medio (ms)": results["mean_latency_ms"][i],
                "Coda Medio (ms)": results["mean_queue_delay_ms"][i],
                "Ritardo Invio Medio (ms)": results["mean_dispatch_delay_ms"][i],
                "Servizio Mediana (ms)": results["median_latency_ms"][i],
                "Coda Mediana (ms)": results["median_queue_delay_ms"][i],
                "Std Dev (ms)": results["std_latency_ms"][i],
                "Min (ms)": results["min_latency_ms"][i],
                "Max (ms)": results["max_latency_ms"][i],
            })
    
    if summary_rows:
        summary_path = GRAFICI_DIR / "Throughput_Summary.csv"
        with open(summary_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=summary_rows[0].keys())
            writer.writeheader()
            writer.writerows(summary_rows)
        print(f"  ✓ {summary_path.name}")


############################################
# MAIN
############################################

def main() -> None:
    print("[GRAFICI] Generazione grafici throughput...\n")
    
    # Verifica se ci sono dati
    results_files = list(RESULTS_DIR.glob("Throughput_Scenario_*.csv"))
    if not results_files:
        print("Errore: Nessun file CSV trovato in Risultati/")
        print(f"Esegui ThroughputDocker.py prima di generare grafici.")
        return
    
    # Grafici per-rate
    print("[PER-RATE GRAPHS]")
    for scenario in [1, 2, 3]:
        results = read_results_csv(scenario)
        if results:
            print(f"Scenario {scenario}:")
            plot_per_rate_graphs(scenario)
    
    # Grafico cumulativo
    print("\n[CUMULATIVE GRAPH]")
    plot_cumulative_graph([1, 2, 3])

    print("\n[BREAKDOWN - TUTTI GLI SCENARI]")
    plot_delay_breakdown_all_scenarios()
    
    # Tabella riepilogativa
    print("\n[SUMMARY TABLE]")
    create_summary_table()
    
    print(f"\n✓ Grafici salvati in: {GRAFICI_DIR}")
    print(f"   Visualizza: {GRAFICI_DIR / 'Throughput_Cumulativo.png'}")


if __name__ == "__main__":
    main()
