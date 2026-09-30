# Paper Figures And Tables Memo

The experiment/setup method and exact data-treatment rules are documented in
`PAPER_EXPERIMENT_METHOD.md`. Use that file when writing the paper's setup and
methodology sections; this memo remains the concise artifact/status index.
Build and campaign operation are documented separately in `README.md`.

Current status: plotting and local campaign support are implemented. The final
latency, throughput, scalability, and handshake YAMLs use five independent
60-second repetitions so their confidence intervals use run means.

## Paper Figures

### P1-P2. Latency Dissection

Output:

```text
P1-P2-latency-dissection.pdf
P1-handshake-latency-dissection.pdf
P2-request-latency-dissection.pdf
P2-request-latency-dissection-dual-scale.pdf
P2-request-latency-dissection-integrated-validation-estimate.pdf
```

One two-panel horizontal stacked-bar figure:

- left: fresh full handshakes for Direct, Baremetal, SGX, Docker, and Docker +
  SGX, with each resumed handshake directly below its Docker strategy;
- right: persistent request/response processing for the five strategies.

The two single-panel files contain the same data for one-column placement. The
handshake stack removes nesting/double-counting; middlebox-server TLS is shown
last for exposition even though it synchronously executes earlier inside the
client-middlebox handshake.

The request stack uses consistent path colors with the handshake panel:
client-server for Direct, client-middlebox for the downstream path, and
middlebox-server for the upstream path. Request and response validation share
one legend item. Final response-body streaming and small uninstrumented
callback gaps are included in the enclosing response path, so no generic
remainder is plotted. Reverse-proxy preparation after response validation is
also part of the client-middlebox response path because the full upstream body
has already been read by the validation handler.

The plot requires the complete matrix and otherwise skips the paper-numbered
output. `configs_latency_dissection.yml` supplies the data. Worker tracing is
enabled only in that controlled campaign. TLMSP now emits standalone
per-request CSVs for Full and No-handler profiles, and the plotter discovers a
copied `<campaign>/tlmsp/latency` directory automatically. TLMSP is the final
bar. Its handshake remains one grey component; its persistent request uses
four grey communication/protocol intervals around request validation,
application processing, and response validation. The two validation intervals
are measured directly around the external handler calls. Full minus No-handler
is retained as an aggregate external-path cross-check.

Because the reference TLMSP handler path is much slower than the DCMB paths,
the plotter also emits an alternative request-only figure. It shows the five
DCMB bars and a right-clipped TLMSP bar on the readable DCMB scale. An arrow
labels the complete TLMSP total, while an inset inside the plot frame shows all
six bars at the true common scale. The ordinary P2 output retains the full
single scale; both versions use identical source data and stack components.

A separately named alternative is an integrated-validation estimate. It
retains TLMSP's measured non-handler components but substitutes
the measured Baremetal Go request- and response-validation durations for the
two TLMSP external-process intervals. Its displayed total is recomputed from
the resulting stack; it is not reported as a measured TLMSP configuration.

### P3. Latency Distributions

Outputs:

```text
P3a-handshake-latency-distribution-linear.pdf
P3a-handshake-latency-distribution-log.pdf
P3b-persistent-request-latency-distribution.pdf
```

The handshake figure includes fresh full handshakes and Container/Container+SGX
resumption. Both linear and logarithmic-density versions are emitted because
full SGX-container handshakes are much slower than the remaining groups. The
request figure uses persistent mode at 10 requests/s. Paper labels are Direct,
Shared, Shared SGX, Container, and Container+SGX; resumption is placed on a
second label line.

After the experiment's normal steady-state and success selection, each violin
removes only extreme upper outliers above `Q3 + 3 * IQR`. The plotting command
prints the resulting threshold and removed count for every group. Short,
unlabelled black bars mark p50, p95, and p99 of the retained samples. The
displayed point is the arithmetic run mean after the same filtering; a
Student-t 95% confidence interval is shown only when multiple runs exist. The
complete five-strategy matrix is required.

Data source: `configs.yml`.

When a copied TLMSP latency campaign is present, Full TLMSP is appended as the
rightmost fresh-handshake and persistent-request violin.

### P4. Instance Startup Time

Output:

```text
P4-instance-startup-time.pdf
```

Log-scale bar plot in this order:

1. Baremetal Process
2. SGX Process
3. Container Worker
4. SGX Container Worker (Standard)
5. SGX Container Worker (SGX-Go)

Process bars end at `[OPERATOR_READY]`. Container bars use the gateway-recorded
`worker_ready_internal` interval from worker creation/start to TCP `:8443`
readiness; gateway startup and gateway-ready milestones are omitted.
The mean is printed above each bar and run-level confidence intervals appear
when repetitions exist. The complete five-category matrix is required.

Data source: `configs_startup.yml`. Use 20-30 starts per category for the final
paper measurement.

### P5. Single-Client Persistent Operating Curve

Output:

```text
P5-single-client-throughput-latency-cpu.pdf
P5a-single-client-throughput-latency.pdf
P5b-single-client-throughput-cpu.pdf
```

Two panels over offered request throughput on a logarithmic x-axis:

- mean end-to-end latency after discarding samples above each run's p99;
- mean total middlebox CPU percentage.

Only one-client persistent runs and canonical full strategies are included.
Direct is omitted from the middlebox CPU panel. The combined figure retains its
title; P5a and P5b are title-free single-panel versions with independent
legends. Invalid points remain connected by the observed strategy line and are
overlaid with red crosses; warnings use amber triangles. Invalid points still
do not contribute to the capacity estimate. Means and confidence intervals use
runs as the statistical unit. Paper labels are Direct, Shared, Shared SGX,
Container, and Container+SGX.

Supporting table:

```text
P5-single-client-capacity.tex
```

Data source: `configs.yml`. The current sweep is dense at low load and at
`1000-5000` requests/s.

Copied Full and No-handler TLMSP throughput campaigns add separate series to
P5a and the capacity table, including mean `tlmsp-mb` process-tree CPU. They are
intentionally omitted from P5b under the TLMSP CPU-scope policy. Set
`INCLUDE_TLMSP_IN_P5A = False` if their latency scale obscures the DC curves.

### P6. Handshake Capacity

There is intentionally no primary P6 plot. The compact paper result is T4.
Detailed offered/achieved and latency curves remain analytical outputs so the
capacity knee and failed points can be audited.

### P7. Client Scalability

Output:

```text
P7-client-scalability.pdf
```

Two panels for Baremetal, SGX, Docker, and Docker + SGX:

- closed-loop achieved throughput versus persistent clients;
- mean open-loop latency at a fixed aggregate 10 requests/s versus clients.

The plot labels these deployment strategies as Shared, Shared SGX, Container,
and Container+SGX. Both x-axes are linear. In addition to the combined figure,
the script writes title-free single-panel versions:

```text
P7a-client-scalability-throughput.pdf
P7b-client-scalability-latency.pdf
```

Client counts are `1, 5, 10, 20, 30, 40` with ordinary x-axis labels. The
40-client Container+SGX closed-loop point is shown without special figure
styling, although its runs had 1.2-3.8% request errors; disclose this in the
paper text. The title-free panels use the P5 canvas, axes margins, and fonts.
The figure is not generated from single-client or partial-strategy campaigns.

Data source: `configs_clients_scalability.yml`.
With `--scalability-rerun-dir`, five matching 30-client Container+SGX 10-rps
runs are pooled with the five original runs for fixed-load latency and T2 only.
The rerun remains a separate campaign, and the five supplemental run summaries
are written to `P7-supplemental-run-summary.csv`. The other strategies and the
closed-loop 30-client point retain five runs.

## Paper Tables

### T1. Selected-Load Resources

Output: `T1-selected-load-resources.tex`.

Reports whole-middlebox CPU and RSS/cgroup memory at one client and persistent
rates 10 and 100 requests/s. Direct is `N/A` because it has no middlebox.

### T2. Clients And Memory

Output: `T2-clients-memory.tex`.

Reports two rows per full-handler deployment at aggregate 10 requests/s with
persistent clients 1, 10, 30, and 40. CPU is the mean after excluding samples
above each run's p99; memory is the per-run p99. Shared uses process-tree RSS,
Shared SGX uses only the in-enclave `go_retained_bytes` sampler, and container
strategies use aggregate gateway-plus-worker cgroup memory. Repeated runs are
reported as the mean with a 95% confidence interval.

### T3. Component Costs

Output: `T3-component-costs.tex`.

Reports fixed, ordered rows for request/response validation on Shared and
Shared SGX, quote generation/verification and DC generation on Shared SGX,
baseline and SGX-Go container-worker creation, and standard-Go/SGX-Go SGX
process startup. Each run contributes one mean; repeated runs are reported as
the mean with a 95% confidence interval. Missing measurements remain `--`.
The SGX startup interval is controller process launch to `[OPERATOR_READY]`, not
a pure enclave-loader microbenchmark.

### T4. Handshake Capacity

Outputs:

```text
T4-handshake-capacity.tex
T4-handshake-capacity-windowed.csv
```

The LaTeX table contains Direct, Baremetal, SGX, Docker, and Docker + SGX full
handshakes plus Docker and Docker + SGX resumed handshakes. Columns are
deployment, handshake type, clients, maximum achieved handshakes/s, mean
handshake latency, and run count. T4 uses a fixed 5-60 s window for both rate
and latency, after the isolated warmup; full-run errors and timeouts still
invalidate a rate point. Each run contributes one mean to the 95% confidence
interval. The windowed CSV contains the selected runs. The DC rows require the
complete fixed-10-client matrix; the optional TLMSP row comes from its separate
open-loop sweep.

Data source: `configs_handshake_capacity.yml`.

Shared 1500/s and Shared SGX 250/s and 500/s use the no-instrumentation reruns.
The superseded run directories and old T4 are retained in the sibling
`2026-09-04_09-42-46_handshake_capacity_replaced_runs_archive` directory.
Regenerate only T4 with:

```bash
python3 benchmarking/plot_latency.py <campaign-dir> --handshake-capacity-only
```

The focused command does not regenerate the general run summary or other plots.
The old all-rate `handshake-capacity-summary.csv` is archived too; a full plot
regeneration will recreate it from the merged runs.

A copied No-handler TLMSP handshake campaign adds an open-loop Full-handshake
row. It is labeled open-loop instead of inheriting the DC campaign's fixed
10-client label.

## Analytical Outputs

These are diagnostics, not primary paper artifacts:

```text
analysis-request-offered-achieved.pdf
analysis-handshake-offered-achieved.pdf
analysis-handshake-latency-vs-offered.pdf
analysis-latency-vs-offered.pdf
analysis-cpu-vs-offered.pdf
analysis-scalability-<strategy>-persistent.pdf
cpu_timeseries.pdf
memory_timeseries.pdf
e2e_timeseries_<strategy>_<handler>.pdf
analysis-resource-summary.tex
```

The handshake analytical plots also require the complete DC fixed-10-client
matrix. An available TLMSP open-loop sweep is added to it. All offered-rate
axes are logarithmic with ordinary numeric labels.
Quality markers are explained once per affected run on stdout:

```text
[PLOT QUALITY] marker=X run=<run> reasons=<reasons>
[PLOT QUALITY] marker=^ run=<run> reasons=<reasons>
```

## Campaign Policy

- Final latency, throughput, scalability, and handshake YAMLs use five
  independent 60-second runs per point.
- Final normal points: at least 5 independent runs.
- Noisy tail/capacity points: prefer 10 runs.
- Startup categories: 20-30 starts.
- Low-rate latency distributions may need longer durations for enough samples.
- Keep failed runs and traces. They are excluded from aggregate statistics but
  remain visible in time series and quality diagnostics.
- One isolated fresh transport error may remain usable as a warning when at
  least ten steady requests succeeded, no timeout/non-2xx occurred, and there
  is at most one matching gateway drop. Its failed sample is still excluded.
- Multi-machine execution, per-node collection, result copy, and clock
  synchronization validation remain a separate final pass.
- On the rootful Podman machine, verify that metadata reports
  `podman_stats_stream`; cgroup v2 is unavailable there, so the Docker cgroup
  collector cannot simply be assumed to work.
