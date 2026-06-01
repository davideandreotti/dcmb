from __future__ import annotations

from pathlib import Path


def _timestamp_owner(ts: str) -> str:
    idx = int(ts[1:])

    # Mapping per Misure base (no orchestrazione gateway/operator).
    client = {1, 10, 20, 21, 22, 23, 24, 27, 28}
    server = {4, 5, 6, 7, 25, 26}
    middlebox = {2, 3, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 37, 38, 39, 40}

    if idx in client:
        return "client"
    if idx in server:
        return "server"
    if idx in middlebox:
        return "middlebox"
    return "cross"


def _edge_owner(start_ts: str, end_ts: str) -> str:
    start_owner = _timestamp_owner(start_ts)
    end_owner = _timestamp_owner(end_ts)
    if start_owner == end_owner:
        return start_owner
    return "cross"


def _enforce_precedence(
    selected: list[tuple[str, float]],
    precedence_pairs: list[tuple[str, str]],
) -> list[tuple[str, float]]:
    """Applica vincoli causali minimi (a prima di b) mantenendo il piu possibile l'ordine attuale."""
    out = list(selected)
    for left, right in precedence_pairs:
        left_idx = next((idx for idx, (tag, _) in enumerate(out) if tag == left), None)
        right_idx = next((idx for idx, (tag, _) in enumerate(out) if tag == right), None)
        if left_idx is None or right_idx is None:
            continue
        if left_idx < right_idx:
            continue
        item = out.pop(right_idx)
        left_idx = next((idx for idx, (tag, _) in enumerate(out) if tag == left), None)
        if left_idx is None:
            out.append(item)
        else:
            out.insert(left_idx + 1, item)
    return out


def render_t1_t10_explosion_chart(
    timestamp_avg_ns: dict[str, float],
    output_path: Path,
    title: str = "Esplosione temporale t1->t10",
    alpha: float = 0.25,
) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        return

    colors = {
        "client": "#d62728",
        "server": "#2ca02c",
        "middlebox": "#1f77b4",
        "cross": "#ffffff",
    }

    parsed: list[tuple[str, float]] = []
    for tag, value in timestamp_avg_ns.items():
        if not (len(tag) >= 2 and tag[0] == "t" and tag[1:].isdigit()):
            continue
        parsed.append((tag, float(value)))

    if not parsed:
        labels: list[str] = []
        y_values_ms: list[float] = []
        edges: list[tuple[str, str, float]] = []
        baseline_label = "t1"
        baseline_ns = 0.0
    else:
        parsed_map = {tag: value for tag, value in parsed}
        has_t1 = "t1" in parsed_map
        selected = parsed

        # L'asse X deve seguire l'ordine temporale reale dei valori medi,
        # non l'indice numerico del timestamp (t17, t21, ...).
        selected = sorted(selected, key=lambda item: (item[1], int(item[0][1:])))

        # Correzioni causali note per i timestamp applicativi server.
        selected = _enforce_precedence(
            selected,
            [
                ("t3", "t25"),
                ("t25", "t26"),
                ("t26", "t9"),
            ],
        )

        baseline_label = "t1" if has_t1 else selected[0][0]
        baseline_ns = parsed_map.get("t1", selected[0][1])
        labels = [tag for tag, _ in selected]
        y_values_ms = [(value - baseline_ns) / 1_000_000.0 for _, value in selected]

        edges = []
        for idx in range(len(selected) - 1):
            start_ts, start_val = selected[idx]
            end_ts, end_val = selected[idx + 1]
            diff_ms = (end_val - start_val) / 1_000_000.0
            edges.append((start_ts, end_ts, diff_ms))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, axis = plt.subplots(1, 1, figsize=(10, 6))

    if len(labels) <= 1:
        axis.text(
            0.5,
            0.5,
            "Dati insufficienti per costruire l'esplosione t1->t10",
            ha="center",
            va="center",
            transform=axis.transAxes,
        )
        axis.set_xlim(0, 1)
        axis.set_ylim(0, 1)
        axis.set_title(title)
        axis.set_xlabel("Events")
        axis.set_ylabel("Latency (ms)")
        axis.grid(True, alpha=0.2)
        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
        print(f"  ✓ {output_path.name}")
        return

    x_positions = list(range(len(labels)))

    owners_in_plot: set[str] = set()

    for idx, (start_ts, end_ts, diff_ms) in enumerate(edges):
        owner = _edge_owner(start_ts, end_ts)
        owners_in_plot.add(owner)
        axis.axvspan(
            idx,
            idx + 1,
            facecolor=colors[owner],
            edgecolor="#9e9e9e",
            alpha=alpha,
            linewidth=1.0,
        )
        """  axis.text(
            idx + 0.5,
            (y_values_ms[idx] + y_values_ms[idx + 1]) / 2.0,
            f"{start_ts}->{end_ts}\n{diff_ms:.3f} ms",
            ha="center",
            va="center",
            fontsize=8,
            color="#202020",
        ) """

    axis.plot(x_positions, y_values_ms, color="#111111", marker="o", linewidth=2)
    axis.set_xticks(x_positions)
    axis.set_xticklabels(labels)
    axis.set_xlabel("Events")
    #axis.set_xlabel("Timestamp (asse categoriale, non temporale)")
    axis.set_ylabel(f"Latency (ms)")
    #axis.set_ylabel(f"Tempo relativo a {baseline_label} (ms)")
    axis.set_title(title)
    axis.grid(True, alpha=0.25)

    legend_order = [
        ("client", "Client"),
        ("middlebox", "Middlebox"),
        ("server", "Server"),
        ("cross", "Latenza inter-componente"),
    ]
    legend_items = [
        Patch(facecolor=colors[key], edgecolor="#9e9e9e", alpha=alpha, label=label)
        for key, label in legend_order
        if key in owners_in_plot
    ]
    axis.legend(handles=legend_items, loc="best")

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"  ✓ {output_path.name}")
