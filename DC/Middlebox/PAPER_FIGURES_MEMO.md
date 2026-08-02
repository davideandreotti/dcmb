# Paper Figures And Tables Memo

The experiment/setup method and exact data-treatment rules are documented in
`PAPER_EXPERIMENT_METHOD.md`. Use that file when writing the paper's setup and
methodology sections; this memo remains the concise artifact/status index.

Current status: plotting and local campaign support are implemented. The YAML
files checked in on 2026-07-31 use one 30-second repetition so the next sweep is
still exploratory. Final confidence intervals require independent repetitions.

## Paper Figures

### P1-P2. Latency Dissection

Output:

```text
P1-P2-latency-dissection.pdf
P1-handshake-latency-dissection.pdf
P2-request-latency-dissection.pdf
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
enabled only in that controlled campaign. A future TLMSP comparison requires a
small event-to-component adapter plus a strategy label/color; the bar layout is
already prepared for another strategy.

### P3. Latency Distributions

Outputs:

```text
P3a-handshake-latency-distribution-linear.pdf
P3a-handshake-latency-distribution-log.pdf
P3b-persistent-request-latency-distribution.pdf
```

The handshake figure includes fresh full handshakes and Docker/Docker + SGX
resumption. Both linear and logarithmic-density versions are emitted because
full SGX-container handshakes are much slower than the remaining groups. The
request figure uses persistent mode at 10 requests/s.

Violin bodies are clipped independently at p95 for readability. The displayed
point is the unfiltered arithmetic run mean; a Student-t 95% confidence interval
is shown only when multiple runs exist. Median, IQR, and p99 ornaments are not
drawn. The complete five-strategy matrix is required.

Data source: `configs.yml`.

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
```

Two panels over offered request throughput on a logarithmic x-axis:

- unfiltered mean end-to-end latency;
- mean total middlebox CPU percentage.

Only one-client persistent runs and canonical full strategies are included.
Direct is omitted from the middlebox CPU panel. Invalid points remain visible
as red crosses, warnings as amber triangles, and neither contributes to the
healthy line or capacity estimate. Means and confidence intervals use runs as
the statistical unit.

Supporting table:

```text
P5-single-client-capacity.tex
```

Data source: `configs.yml`. The current sweep is dense at low load and at
`1000-5000` requests/s.

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

- closed-loop maximum throughput at saturation versus persistent clients;
- mean open-loop latency at a fixed aggregate 10 requests/s versus clients.

Client counts are `1, 5, 10, 50` with ordinary x-axis labels. The figure is not
generated from single-client or partial-strategy campaigns.

Data source: `configs_clients_scalability.yml`.

## Paper Tables

### T1. Selected-Load Resources

Output: `T1-selected-load-resources.tex`.

Reports whole-middlebox CPU and RSS/cgroup memory at one client and persistent
rates 10 and 100 requests/s. Direct is `N/A` because it has no middlebox.

### T2. Clients And Memory

Output: `T2-clients-memory.tex`.

Reports median/peak total memory and memory per client at aggregate 10
requests/s for the scalability client counts. Native/SGX use process-tree RSS;
containers use aggregate gateway plus worker cgroup memory.

### T3. Component Costs

Output: `T3-component-costs.tex`.

Reports delegation retrieval, quote generation/verification, DC generation,
validation/response processing, and worker startup components from the
controlled dissection traces.

### T4. Handshake Capacity

Outputs:

```text
T4-handshake-capacity.tex
handshake-capacity-summary.csv
```

The LaTeX table contains Direct, Baremetal, SGX, Docker, and Docker + SGX full
handshakes plus Docker and Docker + SGX resumed handshakes. Columns are
deployment, handshake type, clients, sustained offered-rate bracket, achieved
handshakes/s, mean handshake latency, and run count. The CSV preserves every
rate and quality decision. The table requires the complete fixed-10-client
matrix.

Data source: `configs_handshake_capacity.yml`.

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

The handshake analytical plots also require the complete fixed-10-client
matrix. All offered-rate axes are logarithmic with ordinary numeric labels.
Quality markers are explained once per affected run on stdout:

```text
[PLOT QUALITY] marker=X run=<run> reasons=<reasons>
[PLOT QUALITY] marker=^ run=<run> reasons=<reasons>
```

## Campaign Policy

- Current YAMLs: one 30-second run per point for validation.
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
