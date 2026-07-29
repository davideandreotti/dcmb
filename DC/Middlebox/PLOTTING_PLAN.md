# Plotting Plan

Goal: build simple plotting scripts/functions that consume one experiment campaign folder and produce latency, throughput, CPU, and startup plots from converted trace/resource CSVs.

Example usage:

```bash
python3 benchmarking/plot_latency.py experiments/2026-06-26_11-46-16_latency_1
```

Expected outputs:

```text
<campaign>/plots/
  e2e_timeseries.pdf
  e2e_violin.pdf
  e2e_latency_samples.csv
  run_summary.csv
  latency_vs_offered.pdf
  cpu_timeseries.pdf
  memory_timeseries.pdf
  cpu_vs_offered.pdf
  throughput_requests_offered_achieved.pdf
  throughput_handshakes_offered_achieved.pdf
  scalability_closed_loop.pdf
  startup_times.pdf
  worker_startup_distribution.pdf
  handshake_request_duration.pdf
  resource_summary.tex
  component_costs.tex
  latency_dissection_<mode>.pdf
```

## Current Status (2026-07-29)

Implemented in `benchmarking/plot_latency.py` without replacing the original
latency plotting pipeline:

- normalized request samples and one-row-per-run `run_summary.csv`;
- run/client failure, non-2xx, late-slot, gateway-drop, and resumption-fallback
  diagnostics, controlled by local `show_failure_annotations` booleans;
- request throughput for persistent plus resumption and handshake throughput for
  fresh plus successful resumption, both restricted to `clients=1`;
- p99 latency and aggregate middlebox CPU versus offered throughput;
- one persistent/resumption scalability PDF per strategy when a campaign has at
  least two client counts;
- one cross-strategy closed-loop scalability plot using `rate=0` runs;
- CPU/RSS time series and `resource_summary.tex` with CPU, median memory, peak
  memory, client count, and offered rate;
- Student-t 95% run-level confidence intervals when at least two independent
  runs are available;
- violin median/IQR/p99 markers, startup log-scale selection, component-cost
  LaTeX output, and horizontal handshake/request latency dissections.
- client/controller quality fields in `run_summary.csv`: request timeouts,
  scheduled rate/ratio, client participation, estimated p99 concurrency,
  in-flight saturation, quality flags, and controller failure reason;
- red plot diagnostics for these quality flags. Two obvious code constants can
  optionally exclude partial-client runs or runs that did not reach offered
  load; exclusions are disabled by default so weak/failed runs remain visible.

New client traces emit `client_tls_resumed`. Resumption throughput counts only
explicit `DidResume=true` handshakes; full-handshake fallbacks are annotated and
not mislabeled as resumed capacity. Old traces without this event cannot prove
resumption and therefore contribute no resumed-handshake count.

The component dissection treats uninstrumented application-server work and
cross-process/network delay as an explicit residual. Splitting that residual is
deferred until the final request server is instrumented. Multi-machine clock
synchronization is also a later pass.

## Inputs

For every run folder inside the campaign, read:

```text
metadata.json
csv/client.csv
csv/gateway.csv
csv/startup.csv
cpu/processes.csv
cpu/containers_total.csv
stdout/client.log
summary.csv
```

Keep metadata-backed runs without `csv/client.csv` as failed/partial annotation
cells and summary rows. They have no latency samples and are excluded from all
aggregate statistics.

Do not support old resource CSV formats in the next implementation pass. New
process resource files should include RSS and thread fields; container aggregate
files should use cgroup memory or `mem_usage_bytes_sum` and aggregate CPU from
`cpu/containers_total.csv`. Virtual memory is not a paper metric.

## End-To-End Latency Extraction

Group client trace events by request `id`.

Use these events:

```text
client_request_start
client_response_done
client_request_error
```

A request is valid when:

- `client_request_start` exists
- `client_response_done` exists
- `client_response_done.arg` is a 2xx HTTP status
- no `client_request_error` exists for that request

Compute:

```text
latency_ns = client_response_done.timestamp_ns - client_request_start.timestamp_ns
latency_ms = latency_ns / 1e6
elapsed_s  = (client_request_start.timestamp_ns - first_request_start_in_run) / 1e9
```

Failed requests are excluded from latency plots but counted for annotations.

## Steady-State Window

For throughput/latency summary plots, use a steady-state request set:

- ignore request IDs starting with `warmup-`
- sort remaining requests by `client_request_start`
- exclude the first 10 remaining real requests from steady-state calculations
- include requests whose request-start timestamp is inside the measurement window
- use request-start time for the end cutoff, so requests started before the end still count if they finish after it
- use successful 2xx response events only for achieved-throughput and latency statistics

This cuts the explicit warmup request and a short request-count-based transient.
Keep the value as one obvious plotting constant (`steady_state_skip_requests = 10`)
so the final analysis can change it without touching extraction logic. This is a
global steady-state trim; handshake analysis for persistent/resumption clients
must additionally identify and treat the first request of each logical client
separately.

For summary plots, keep this global ten-request trim even when several
persistent clients are active. Handshake-distribution plots are different: use
the logical-client ID helper to identify each client's initial full handshake.

## Persistent Mode

Use `metadata.json`:

```text
parameters.mode
parameters.clients
parameters.rate
parameters.deployment.name
parameters.iteration
```

For persistent and resumption modes:

- Keep all valid requests in the raw time-series view unless the plot explicitly says it is steady-state only.
- For steady-state violin/summary plots, use the same global window rule above.
- For handshake-specific plots, exclude the first full handshake of every
  logical resumption client when measuring resumed-handshake latency.

First-pass persistent client identification:

- Request IDs look like `lambrate-1-23`.
- Remove the final `-<request_number>` suffix.
- Treat the remaining prefix as the logical persistent client ID.

Example:

```text
lambrate-1-23 -> lambrate-1
```

If the ID shape changes later, update this helper only.

## Processed Data

Write one normalized CSV:

```text
plots/e2e_latency_samples.csv
```

Suggested columns:

```text
campaign
run_name
deployment
mode
clients
rate
iteration
request_id
elapsed_s
latency_ns
latency_ms
status
in_violin
```

For failed requests, include a row with `status=failed` and empty latency fields if practical. This keeps failure annotations reproducible.

This CSV may be large if the campaign has many requests, but it is useful for inspection and avoids reparsing raw traces for every plot. If size becomes annoying later, add an option to skip it or write `.csv.gz`.

## Plot 1: Time Series

Output:

```text
plots/e2e_timeseries.pdf
```

One subplot per run in the campaign.

Each subplot:

- x-axis: `elapsed_s`
- y-axis: `latency_ms`
- linear scale
- all valid requests plotted as unconnected dot samples
- dot samples should be light blue
- overlay a time-window moving average line in dark blue
- add an average-latency annotation on the right side, vertically aligned with the run's average latency
- title includes deployment, mode, clients, rate, and run number
- if failed requests exist, add a red annotation such as `failed: 3`
- keep the failure annotation as a tiny obvious code block that can be manually commented out; do not add a dedicated CLI flag for it

## Plot 2: Violin

Output:

```text
plots/e2e_violin.pdf
```

Pool repeated runs for now.

Group by:

```text
deployment + mode
```

Example labels:

```text
baremetal
fresh

baremetal
persistent

docker_gateway
fresh

docker_gateway
persistent
```

Rules:

- use only valid requests
- exclude `warmup-*`
- exclude the first 10 real requests after warmup
- use a linear y-axis
- remove the black violin body border
- print average latency for each violin
- place the average text to the right of the corresponding violin, vertically aligned with the average marker/line
- support an optional percentile clip for violin samples only, e.g. `percentile_clip = 99`; keep it as an easy-to-edit local variable and make it clear in the title if enabled
- add a red failure annotation per group if failures exist

## Script Shape

Keep the plotting script easy to edit manually. Prefer simple functions over deep abstractions:

- Do not grow a large CLI for every visual detail.
- Keep editable plotting knobs close to the specific plot function, or in `main()` when they describe output paths.
- Do not force one global plotting configuration for every figure; time-series and violin plots may need different titles, labels, colors, sizes, y-limits, and annotation behavior.
- Keep the data extraction functions separate from plotting functions so later latency-breakdown plots can reuse the processed samples.

```python
def load_campaign(campaign_dir): ...
def load_run(run_dir): ...
def extract_client_latencies(client_csv, metadata): ...
def write_samples_csv(samples, path): ...

def plot_timeseries(samples, run_summaries, output_path):
    figure_size = (12, 8)
    y_label = "End-to-end latency (ms)"
    dot_color = "#8ecae6"
    moving_average_color = "#023047"
    moving_average_window_s = 1.0
    show_failure_annotations = True  # comment out locally if not desired
    ...

def plot_violin(samples, run_summaries, output_path):
    figure_size = (8, 5)
    y_label = "End-to-end latency (ms)"
    percentile_clip = None  # set to 99 to clip each violin at p99
    show_failure_annotations = True  # comment out locally if not desired
    ...

def main():
    timeseries_filename = "e2e_timeseries.pdf"
    violin_filename = "e2e_violin.pdf"
    samples_filename = "e2e_latency_samples.csv"
    ...
```

Use:

```text
Python stdlib csv/json
matplotlib
```

## Final Paper Plotting Pass

The existing script is a useful first pass. Each invocation receives exactly
one selected campaign directory. That campaign may live under either:

```text
DC/Middlebox/experiments/
DC/bovisa/experiments/
```

Do not merge those roots implicitly: the chosen campaign directory contains all
data for that analysis. Failed/partial runs remain visible in latency time
series and aggregate plots with red markers or annotations. Do not include
partial failed runs in aggregate means, percentiles, confidence
intervals, or achieved-throughput calculations unless they are explicitly
selected as valid completed measurement windows.

### 1. Throughput, Latency, And CPU Curves

Use only `clients=1` for the normal offered-rate comparison. The strategy set is:

```text
Direct
Bare metal
Gramine SGX
Container
Container + SGX-Go
```

Use the full handler and delegated-credential reuse whenever the deployment can
reuse it. Keep no-reuse, empty-handler, and quote-cost results as supporting
measurements rather than additional main-figure strategy lines.

Produce two explicit throughput families:

- request throughput: persistent plus container resumption, offered versus
  achieved successful requests/s, with a `y=x` reference;
- handshake throughput: fresh plus container resumption, offered versus
  achieved successful handshakes/s, with a `y=x` reference;
- offered transactions/s versus p99 end-to-end latency;
- offered transactions/s versus aggregate middlebox CPU percentage;
- failure/drop/late-slot annotations on the affected curves.

Fresh mode is one full TLS handshake per transaction. Persistent mode has one
initial full handshake per logical client and then request-only transactions.
Resumption mode opens a connection and performs a resumed handshake per
transaction after each logical client's first full handshake.

For the first final pass, include resumption in both relevant families so it is
easy to compare and later disable in one place:

- persistent and resumption in the request-throughput figure;
- fresh and resumption in the handshake-throughput figure.

Resumption exists only for container strategies in the current experiments.
Use dashed resumption lines. In fresh and resumption mode one transaction opens
one TLS connection. Count successful `client_tls_done` events for handshakes/s,
and exclude the first full-handshake request of each resumption client.
Persistent mode does not belong in steady-state handshakes/s because it performs
no handshake for most requests.

The existing throughput aggregation must be changed to:

- filter normal comparison plots to `clients == 1`;
- preserve `clients` as a grouping key for scalability plots;
- accept `resumption` instead of silently dropping it;
- avoid combining reuse/no-reuse runs under one unlabeled strategy;
- use configured offered rate as the x-axis;
- annotate client `late_slots`, transport errors, non-2xx responses, and gateway
  drops so load-generator saturation or a failed run is never silent.

Read `late_slots`, scheduled/started debug rates, non-2xx counts, and transport
errors from the client's machine-readable `THROUGHPUT_DATA` line in
`stdout/client.log`; these quantities are not all represented as per-request
trace events today.

The client skips elapsed rate slots instead of issuing catch-up requests. There
is therefore no need for an additional actual-started curve in the paper
figures: missed slots already reduce achieved/offered throughput. Keep
`scheduled_rps` and `started_rps` in raw summaries only for debugging.

### 2. End-To-End And Handshake Distributions

At a common unsaturated offered rate, use one panel per connection mode.
Recommended final presentation:

- violin distribution for per-transaction samples;
- embedded median/IQR box;
- explicit p99 marker;
- no silent percentile clipping in the final figure.

Use separate handshake distributions for fresh full handshakes and resumed
handshakes. Persistent initial-handshake samples may be included as a separate
category, but do not imply that every persistent request includes a handshake.

### 3. Resource Presentation

Keep CPU as percentage and state in the paper that 100% is one fully occupied
logical CPU, so totals may exceed 100%. Exclude direct from a plot explicitly
labeled "middlebox CPU" because direct has no middlebox. A separate total-system
CPU figure would require summing client, server, certserver, gateway, worker,
runtime, and SGX-service costs on all involved nodes.

For memory, produce a LaTeX table at selected offered rates and client counts.
Report:

```text
strategy, mode, clients, offered rate,
steady median memory, steady peak memory
```

Write this as `plots/resource_summary.tex`, using `booktabs`-compatible rows.
Keep memory time series as diagnostic/appendix figures. Use process-tree RSS for
native/Gramine and cgroup memory for containers. Do not report VMS. RSS/cgroup
memory is not the same as SGX EPC usage and must not be labeled as enclave
memory.

### 4. Startup Figures

Use a dedicated repeated startup campaign and plot distributions for:

- native middlebox process to ready;
- standard Gramine middlebox to ready;
- modified SGX-Go middlebox under Gramine to ready;
- plain container create to worker ready;
- SGX/SGX-Go container create to worker ready;
- system-off to gateway plus first worker ready.

Do not require a full-pool-ready metric for the main figure. Worker startup under
parallel creation should record the configured batch size because contention can
change the distribution.

The repository currently has a dedicated Gramine container image only for the
modified SGX-Go worker. A standard Gramine-in-container startup category requires
a separate worker image before that comparison can be collected. That image is
strictly for the startup campaign and must not be used in runtime throughput,
latency, or resource experiments.

### 5. Client Scalability

Create one figure per deployment strategy. Within each figure, draw one line per
client count for:

- offered versus achieved transactions/s;
- offered versus p99 latency;
- offered versus CPU percentage.

From the same normalized data, also derive closed-loop capacity versus client
count as a compact cross-strategy summary. Run this for persistent mode first.
Add a resumption scalability campaign because concurrent resumed handshakes are
architecturally meaningful. Fresh mode uses `max-in-flight`, not `clients`, so it
needs a concurrency-limit sweep rather than a logical-client sweep.

### 6. Statistical Unit And Repetitions

The independent run, not each request, is the statistical unit. Compute each
run's throughput/latency/resource statistic first, then aggregate across runs.

- Use at least 5 independent runs per normal throughput point.
- Prefer 10 for p99 latency or visibly noisy points.
- Use 20-30 independent starts per startup category.
- Randomize or rotate matrix order across repetitions.
- For low offered rates, increase duration because a 60-second run at 1 rps is
  not enough for a stable p99.

Pandas is not required for the first version. Avoid seaborn unless it is clearly useful and already installed. Matplotlib's `violinplot` is enough for the first version.

## CLI

Initial CLI:

```bash
python3 benchmarking/plot_latency.py <campaign_dir>
```

Do not add more CLI options for plot selection or styling. Keep figure inclusion,
labels, colors, filenames, failure annotations, and optional strategy lines as
obvious constants near `main()` or inside the corresponding plot function.

## Implemented Plotting Pass Details

The following sections document the implemented functions and their intended
interpretation. The component-cost section remains planned.

### Processed Summary

Write a small run-level summary:

```text
plots/run_summary.csv
```

One row per run:

```text
campaign
run_name
deployment
mode
clients
offered_rps
achieved_rps
success
failed
late_slots
run_status
p99_latency_ms
mean_cpu_percent
peak_cpu_percent
startup_process_ready_ms
startup_first_worker_ready_ms
```

This table should be small. Do not duplicate all packet samples into it. Keep packet-level data in `e2e_latency_samples.csv` and raw trace CSVs.

### Latency Time Series With Drops

Extend the existing `e2e_timeseries.pdf`:

- successful requests remain light-blue dots
- moving average remains dark blue
- failed/dropped requests are shown as small red ticks at the top edge of the subplot
- y-axis starts at zero

The red ticks and text annotation should be controlled by one local hardcoded
boolean such as `show_failure_annotations = True`, easy to change or comment
out. Do not add an environment variable or CLI option. Include client errors,
non-2xx responses, late slots, gateway drops, and failed/partial run status when
the source data provides them.

### Offered vs Achieved Request Throughput

Output:

```text
plots/throughput_requests_offered_achieved.pdf
```

Rules:

- x-axis: offered requests/sec from metadata/client config
- y-axis: achieved requests/sec
- achieved throughput counts successful 2xx response events only
- use the steady-state window rule above
- use `clients == 1`
- include persistent and container resumption lines
- group lines by deployment, mode, and reuse policy
- mark failed/partial runs in red rather than silently pooling them

### Offered vs Achieved Handshake Throughput

Output:

```text
plots/throughput_handshakes_offered_achieved.pdf
```

Rules:

- x-axis: offered new TLS connections/sec from metadata/client config
- y-axis: successful completed handshakes/sec from `client_tls_done` events with
  a success argument
- use `clients == 1`
- include fresh full handshakes and container resumed handshakes
- exclude the first full-handshake request for each resumption client
- keep HTTP response failures as separate red annotations; a completed TLS
  handshake still counts even if the later HTTP request fails
- use the separate `client_tls_resumed` event emitted from
  `tls.ConnectionState.DidResume`; preserve the existing TLS-done error argument
  and annotate full-handshake fallbacks instead of counting them as resumed
- keep a local hardcoded boolean that can hide resumption from this figure later

### Offered Throughput vs End-To-End Latency

Output:

```text
plots/latency_vs_offered.pdf
```

Rules:

- x-axis: offered requests/sec
- y-axis: p99 end-to-end latency in ms
- use successful requests only
- use the steady-state window rule above
- group by deployment and mode

### CPU Time Series

Output:

```text
plots/cpu_timeseries.pdf
```

Rules:

- baremetal/SGX: use `cpu/processes.csv`
- direct: skip from middlebox CPU plots
- Docker: use `cpu/containers_total.csv`
- process CPU percent is computed from deltas of `user_time_s + system_time_s` over wall-clock sample deltas
- for Gramine/wrapper-style deployments, use process-tree CPU/memory where available
- Docker CPU uses `cpu_perc_sum`
- CPU percent may exceed 100% on multi-core workloads

For process deployments, identify the middlebox process by excluding `server`, `certserver`, and `client` roles rather than relying on old role names.

### Memory Time Series

Output:

```text
plots/memory_timeseries.pdf
```

Rules:

- baremetal/SGX: use `cpu/processes.csv`
- direct: skip from middlebox memory plots
- Docker: use `cpu/containers_total.csv`
- process deployments use `rss_tree_bytes` when available, falling back to `rss_bytes`
- Docker uses `mem_usage_bytes_sum`
- y-axis should be in MiB
- keep this as a diagnostic time-series plot; do not overinterpret Go RSS as exact live heap usage

### Offered Throughput vs CPU

Output:

```text
plots/cpu_vs_offered.pdf
```

Rules:

- x-axis: offered requests/sec
- y-axis: mean CPU percent over the steady-state window
- skip direct
- Docker uses whole gateway/worker machinery from `containers_total.csv`
- baremetal/SGX use the middlebox process tree where available

### Startup Time

Output:

```text
plots/startup_times.pdf
```

Input:

```text
csv/startup.csv
```

Rules:

- bar plot by deployment strategy
- repeated runs should show mean plus confidence interval, and ideally individual points if run count is small
- baremetal/SGX: process start to `[OPERATOR_READY]`
- Docker: plot both gateway ready and gateway plus first worker ready
- direct has no middlebox startup and should be skipped

### Worker Startup Distribution

Output:

```text
plots/worker_startup_distribution.pdf
```

Input:

```text
csv/startup.csv
```

Rules:

- Docker only
- use `worker_ready_internal` rows
- violin or boxplot of per-worker `startup_ms`

### Handshake vs Request Duration

Output:

```text
plots/handshake_request_duration.pdf
```

Rules:

- separate panels or separate figures for fresh and persistent modes
- group by deployment strategy
- use two adjacent bars per deployment: handshake duration and request duration
- do not stack the bars in the first implementation
- keep deeper breakdowns such as delegation, attestation, validation, and upstream request for a later plotting pass

Use the already agreed event windows:

```text
handshake = client_request_start -> client_tls_done
request   = client_request_sent  -> client_response_done
```

### Component Cost Table

Write a LaTeX table to:

```text
plots/component_costs.tex
```

Use one row per component/deployment/mode and report at least `n`, median, and
p95. Derive the rows from paired events with matching linked flow IDs:

```text
delegated credential: middlebox_delegation_fetch -> middlebox_delegation_fetched
quote generation:     middlebox_attestation_start -> middlebox_attestation_done
quote verification:   certserver_quote_verify -> certserver_quote_done
DC generation:         certserver_generate -> certserver_generate_done
request validation:   middlebox_validation_start -> middlebox_validation_done
response processing:  middlebox_response_validation_start -> middlebox_response_validation_done
worker startup:        gateway_container_create -> gateway_container_ready
```

The table generator should emit `booktabs`-compatible LaTeX directly. A small
CSV may remain as an internal normalized intermediate, but the paper artifact is
the `.tex` file.

## Gitignore

Add a gitignore rule for generated plot folders:

```gitignore
DC/Middlebox/experiments/*/plots/
```

If the normalized samples CSV lives under `plots/`, it will be ignored together with generated PDFs. That is acceptable because it is generated data.

## Proposed Implementation Sequence

Keep the next work in reviewable passes:

1. **Done:** Normalize one campaign into run-level records: deployment, mode, clients,
   reuse, offered rate, run status, late slots, failures, successful requests,
   successful handshakes, p99 latency, and resource-file paths.
2. **Done:** Implement the two `clients=1` throughput figures and failure annotations,
   then the latency/CPU curves. This validates mode and strategy grouping before
   adding more figures.
3. **Done:** Add per-strategy client-scalability figures and the LaTeX resource table.
4. **Done:** Replace per-request schema compilation with the agreed startup cache and add
   schema-initialization trace markers before collecting final validation runs.
5. **Done with target-host fallback:** Use a persistent rootful Podman stats
   stream; verify it on Bovisa. Cgroup v2 is unavailable there.
6. **Mostly done:** Trace-ID/connection/delegation bindings, component-cost
   table, and latency dissection are implemented. Request-server events remain
   the final touch, so application processing is currently part of the residual.
7. **Done:** The startup-only standard Gramine image and repeated startup
   campaign are separate from runtime campaigns.
