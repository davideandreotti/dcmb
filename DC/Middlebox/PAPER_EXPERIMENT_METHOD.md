# Paper Experiment And Plot Method

This document is the methods-oriented handoff for writing the paper. It
describes what the checked-in benchmark campaigns measure, how samples are
filtered, what each paper artifact contains, and why those choices were made.
Implementation status and filenames are tracked separately in
`PAPER_FIGURES_MEMO.md`.

The description matches the code and YAML files as of 2026-08-01. Final paper
runs must preserve their copied `campaign.yml`, `metadata.json`, raw traces,
resource CSVs, and controller `summary.csv`; those files take precedence over
this document if a later campaign changes a parameter.

## 1. Experimental System

### Components

Each normal run starts these roles locally unless the deployment is Direct:

| Role | Endpoint | Purpose |
|---|---|---|
| Client | n/a | Generates fresh, persistent, or resumed TLS/HTTP traffic. |
| Gateway | `:9443` | Selects a ready container worker and splices TCP byte streams. Container strategies only. |
| Middlebox worker | `:8443` | Terminates client TLS, validates HTTP messages, and proxies to the application server. |
| State endpoint | `:18080` | Worker health/readiness support; gateway readiness uses TCP `:8443`. |
| Certificate server | `:5000` | Verifies an optional SGX quote and generates an Ed25519 delegated credential. |
| Application server | `:8000` | TLS 1.3, HTTP/1.1 request server for `/function/init`. |

The current local YAMLs launch all host processes on one machine. Containers
reach the host services through `10.79.1.175`, the host's externally reachable
address, rather than container-local `localhost`. The Direct strategy connects
the client directly to the TLS application server and does not start the
certificate server.

The application server accepts a nonempty bearer token and returns a small JSON
response. The middlebox receives this benchmark request:

```http
POST /function/init HTTP/1.1
Authorization: Bearer token
Content-Type: application/json
X-Testing: 1

{"operation":"init"}
```

SNI is fixed to `server`. The `X-Testing` header selects dummy token validation
so the benchmark measures the validation pipeline without depending on an
external identity provider. The full handler still matches the endpoint,
validates the JSON schema, checks the state transition/code policy, and
processes the response. Schemas are initialized at middlebox startup. JWKS
material is initialized lazily only for real JWT validation and is not touched
by the canonical `X-Testing` requests. Session state is keyed by `X-Client-ID`,
uses per-entry locking, has a 2-second TTL, and is lazily cleaned every 1000
requests. The empty-handler binary is an ablation and is not a primary paper
strategy.

### Deployment Strategies

| Paper label | Implementation |
|---|---|
| Direct | Client to application server, with TLS but no middlebox. |
| Baremetal | One shared native Go middlebox process. |
| SGX | The standard Go middlebox under Gramine SGX. Quote emission is enabled in normal SGX campaigns. |
| Docker | Gateway plus native disposable/persistent worker containers. |
| Docker + SGX | Gateway plus SGX-Go workers running under Gramine inside containers. Quote emission is enabled. |

Baremetal and shared SGX support multiple downstream TLS sessions in one
process. Each accepted downstream TLS session owns one upstream TLS/HTTP/1.1
connection to the application server. Persistent downstream sessions therefore
reuse their corresponding upstream connection; fresh and resumed downstream
connections establish new upstream sessions.

For container deployments, the gateway leases a worker to an incoming TCP
connection. Persistent clients retain the worker for their connection. Fresh
traffic exercises disposable workers and background refill. Resumption uses a
shared deterministic TLS ticket identity so a new worker can resume a session
issued by another worker.

When `prewarm_for_clients` is enabled, the controller waits for the full initial
pool before starting the client:

- fresh: one worker, intentionally exposing steady-state refill behavior;
- persistent: `clients + 1` workers, where the extra worker absorbs the
  isolated warmup connection;
- resumption: one worker per logical client.

### Host And CPU Controls

Before the experiments, CPU performance mode was applied with the repository
script:

```bash
cd /home/bonsai/dcmb
sudo ./scripts/cpu-performance-mode.sh on
./scripts/cpu-performance-mode.sh status
```

The actual filename uses hyphens, not `cpu_performance_mode.sh`. The script:

- sets every cpufreq policy governor to `performance`;
- sets energy-performance preference to `performance` when supported;
- disables CPU idle-state indexes 2 and 3 by default;
- saves the prior settings so `off` can restore them.

The status observed while preparing this document showed all six CPU policies
in performance mode and idle states C1E/C3 disabled. Record the status output in
the final experiment notes because kernel or firmware changes can alter the
available policies/states.

Current host snapshot, to be rechecked for the final campaign:

| Item | Value |
|---|---|
| CPU | Intel Core i5-8400, 6 logical CPUs, 2.80 GHz nominal, 4.00 GHz maximum |
| Memory | 31 GiB |
| Kernel | Linux 6.8.0-124-generic, x86-64 |
| Gramine | `1.8post~UNRELEASED`, revision `1f539cca...` |

Also record the final Docker/Podman, Go/custom toolchain, AESM/DCAP, and PCCS
versions. These are not reliably recoverable from the result CSVs.

## 2. Workload And Connection Modes

All configured rates are aggregate offered rates, not rates per client.

| Mode | Connection behavior | Meaning of one transaction |
|---|---|---|
| Fresh | Every request creates and closes a new TCP/TLS connection. Concurrency is bounded by `max-in-flight`. | One full handshake plus one HTTP request/response. |
| Persistent | Each logical client serially sends requests over its own HTTP/1.1 keep-alive connection. | Usually one request/response; only priming performs the connection handshake. |
| Resumption | Each logical client closes after every request but keeps a one-entry TLS session cache. | One resumed handshake plus one HTTP request/response. |
| Closed loop | Persistent clients immediately issue their next serial request after receiving the previous response (`rate: 0`). | Saturated capacity, not representative latency at a controlled load. |

For open-loop persistent/resumption traffic, the total rate is divided evenly
among logical clients. Client start offsets are distributed over one global
inter-arrival interval. A client that cannot meet its next serial slot skips
late slots rather than issuing a catch-up burst. Consequently, achieved rate
can fall below offered rate without building an unbounded client-side queue.
The timed run has one shared deadline context. Persistent/resumption clients
each reuse one pacing timer; fresh mode reuses one timer in its central
dispatcher while retaining one goroutine per request and the `max-in-flight`
bound. All pacing uses absolute deadlines, and the context stops only future
scheduling rather than cancelling requests already in flight.

### Warmup And Measurement Start

There are three separate stabilization mechanisms:

1. After deployment readiness, the controller waits `warmup_s` (normally one
   second) before starting resource monitors and the client.
2. Fresh and persistent modes issue one isolated `warmup-*` request, close its
   transport, and wait one second. Resumption skips this isolated request.
3. Persistent and resumption modes prime every logical client 100 ms apart and
   wait at a barrier before starting the timed interval. Persistent retains the
   primed connection; resumption retains only the TLS session cache.

The middlebox deliberately discards the first fetched delegated credential.
The isolated/per-client priming sequence therefore warms process/network caches
without accidentally carrying that first credential into the wrong measured
case. Canonical reuse experiments measure steady cache-hit behavior; no-reuse
dissection runs fetch a delegation on every full handshake.

Warmup trace IDs (`warmup-*` and `warmup-client-*`) are never measurement
samples. The plotter then sorts measured request starts and removes the first 10
real requests. This request-count trim removes the remaining startup transient
without discarding an arbitrary number of seconds at low offered rates.

The steady-state end is based on request start: a successful request counts if
it starts before the configured measurement-window end, even if its response
finishes just after it. Achieved throughput is successful 2xx completions in
this steady window divided by the resulting steady-window duration.

## 3. Traces, Resources, And Statistics

### Binary Event Traces

Tracing is compiled with the `trace` build tag. Each role has one asynchronous
binary writer with a 100,000-event channel and a 1 MiB buffered file writer.
Normal throughput campaigns use `trace_drop_on_full: true` to avoid blocking
the measured path; the controlled dissection campaign uses `false` so no
component event is dropped. The writer flushes on graceful `Stop`, not on a
periodic timer. Runtime container campaigns disable per-worker traces; the
single-worker dissection campaign enables them.

The controller converts every nonempty `.bin` to CSV after graceful shutdown.
Trace IDs are transported in `X-Trace-ID`. Additional connection/delegation IDs
bind client, gateway, worker, certificate-server, and application-server
events. Local cross-process timestamps share the host clock. Multi-machine
component subtraction requires clock synchronization and remains a separate
validation step.

### CPU And Memory

Resources are sampled every 200 ms after deployment readiness and controller
warmup. Summary resource statistics start no earlier than both:

- the trace-derived steady-state start; and
- two seconds after the first resource sample.

Shared/Shared-SGX CPU is calculated from process-tree user+system CPU-time
deltas divided by wall-time deltas. Shared memory is process-tree RSS. Shared
SGX memory uses the in-enclave Go sampler's `go_retained_bytes` and has no RSS
fallback. Container strategies use aggregate gateway-plus-worker cgroup CPU
and memory; the CSV retains the field names `cpu_perc_sum` and
`mem_usage_bytes_sum`. CPU 100% means one fully occupied logical CPU and totals
may exceed 100%.

The client-scalability resource table uses full-handler persistent runs at 10
requests/s. It reports the CPU sample mean after excluding values above each
run's p99 and reports p99 memory. Duration-integrated CPU utilization remains a
planned final accounting refinement.

These are middlebox-machinery resources. They exclude the client, application
server, certificate server, Docker daemon, AESM, PCCS, and other host services.
Shared SGX Go-retained memory is not EPC usage. Direct therefore has no
middlebox CPU or memory value.

Local Docker uses cgroup v2. The rootful Podman machine does not expose the same
cgroup-v2 setup, so it uses a persistent `podman stats --all` stream with a
no-stream fallback. Before using Podman results, verify `metadata.json` records
`podman_stats_stream`.

### Successful Samples And Point Quality

A latency sample requires `client_request_start`, a 2xx
`client_response_done`, and no `client_request_error`. Failed samples do not
enter means, distributions, throughput, or confidence intervals, but remain in
time-series diagnostics.

A run point is invalid if it has a controller/client failure, non-2xx response,
request error/timeout, gateway drop, resumption fallback, partial logical-client
participation, serial-client concurrency shortage, or a material
scheduled/achieved deficit. One isolated fresh-mode transport error is retained
as a warning when at least ten steady requests succeeded, no timeout/non-2xx
occurred, and there is at most one matching gateway drop. The failed request is
still excluded from every metric. A deficit is material when it exceeds the
larger of 2% of offered rate and approximately one boundary slot per steady
window; the tolerated failure adds one further boundary slot. Smaller boundary
deficits, late slots, and in-flight-limit observations are warnings.

Invalid points appear as red crosses and warnings as amber triangles. They are
not connected into healthy curves or used to select capacity. The plotter also
prints every marker and reason to the terminal.

Independent runs, not packets, are the statistical units for plotted means and
Student-t 95% confidence intervals. With one run no confidence interval is
shown. Use at least 5 runs per normal point, 10 for noisy capacity/tail points,
and 20-30 process/container starts per startup category.

## 4. Paper Figures And Data Treatment

### P1-P2: Latency Dissection

Files: `P1-P2-latency-dissection.pdf`,
`P1-handshake-latency-dissection.pdf`, and
`P2-request-latency-dissection.pdf` from `configs_latency_dissection.yml`.

The left panel uses `client_request_start` to `client_tls_done`, so the
"handshake" total deliberately includes client TCP setup. Each horizontal bar
is divided into:

| Component | Event interval/derivation |
|---|---|
| Client-server TLS | Complete client-observed handshake for Direct. |
| Client-middlebox TLS | Client-observed handshake remainder after subtracting upstream setup and delegation retrieval. TCP and gateway setup are included; the component is split around delegation retrieval. |
| DC retrieval | Middlebox delegation-fetch interval excluding quote generation and verification. It is split around those nested operations and includes DC generation, transfer, and parsing. |
| Quote generation | Middlebox attestation start to done for the delegation ID. |
| Quote verification | Certificate-server DCAP verification start to done. |
| Middlebox-server TLS | Middlebox upstream TCP dial start through upstream TLS completion. It is rendered last for presentation, although the synchronous implementation executes it earlier inside the client-middlebox handshake. |

It shows five fresh no-reuse full handshakes plus Docker and Docker + SGX
resumption. No-reuse is intentional: the figure exposes delegation,
attestation, and verification costs instead of hiding them behind a cache hit.

The right panel uses `client_request_sent` to `client_response_done` for a
steady persistent request. It contains:

| Component | Event interval/derivation |
|---|---|
| Client-server path | Direct request sent to application-server request start, then application-server response start through client response completion. |
| Client-middlebox path | Client request sent to middlebox validation start, then response validation completion through client response completion. This includes reverse-proxy preparation before its first downstream write. |
| Validation | Middlebox request-validation and response-validation intervals. Both use the same stack color and legend entry. |
| Middlebox-server path | Request validation done to application-server request start, and application-server response start to response-validation start. |
| Application handler | Application-server request start to response start. |

Components are averaged within each run and then across runs. The stacked bars
explain where latency occurs; they are not throughput/capacity results. The
reverse proxy does not expose an event for the instant at which the complete
upstream body has been read. Therefore, the final streamed-body interval is
assigned to the enclosing client-middlebox response path (or client-server
response for Direct). Small uninstrumented callback-boundary gaps are absorbed
by that final path so every stack equals the client-observed request duration;
there is no separate remainder category.

### P3: Latency Distributions

Files:

```text
P3a-handshake-latency-distribution-linear.pdf
P3a-handshake-latency-distribution-log.pdf
P3b-persistent-request-latency-distribution.pdf
```

P3a uses fresh full handshakes at 1 handshake/s and Docker/Docker + SGX resumed
handshakes at 10 handshakes/s. P3b uses one-client persistent requests at 10
requests/s. All use the canonical full handler and successful steady samples.

For readability, each violin body is clipped independently at its p95. The
displayed dot is still the unfiltered arithmetic run mean, and confidence
intervals are computed over unfiltered run means. The logarithmic version
estimates density in log10 latency space; it exists because SGX-container full
handshakes can otherwise compress all low-latency distributions at the bottom
of the linear plot. Choose one P3a version for the final paper, not both unless
space permits.

### P4: Instance Startup

File: `P4-instance-startup-time.pdf` from `configs_startup.yml`.

The log-scale bars compare native process, shared Gramine SGX process, plain
container, standard-Gramine SGX container, and SGX-Go container startup. Means
are printed above bars; confidence intervals use independent starts.

Process bars measure controller spawn to `[OPERATOR_READY]`. Container bars use
`worker_ready_internal`, measured from worker-container creation/start to the
worker's TCP `:8443` readiness. Gateway startup and gateway readiness are not
included in the container bars.

Startup ends at listener/readiness and excludes client-triggered delegation and
attestation. Full-handler startup includes schema initialization; JWKS loading
is excluded because canonical requests use dummy token validation.

### P5: Single-Client Persistent Operating Curve

Files from `configs.yml`:

```text
P5-single-client-throughput-latency-cpu.pdf
P5a-single-client-throughput-latency.pdf
P5b-single-client-throughput-cpu.pdf
```

The panels show p99-filtered mean steady end-to-end latency and mean total
middlebox CPU versus aggregate offered requests/s for one persistent client.
For each run, successful steady-state latency samples above that run's p99 are
discarded before calculating its plotted mean. The x-axis is logarithmic. This
intentionally reveals the physical serial limit of one connection; it is not a
global capacity claim. Direct is omitted from the CPU panel because there is no
middlebox. Invalid observations remain connected by the strategy line but are
overlaid with red crosses; warnings use amber triangles. The split P5a/P5b
figures are title-free and have independent legends.

`P5-single-client-capacity.tex` reports the highest usable tested point for
each strategy. Closed-loop points are not added to P5 by default because their
latency is measured under deliberate saturation.

### P7: Client Scalability

File: `P7-client-scalability.pdf` from
`configs_clients_scalability.yml`.

For Shared, Shared SGX, Container, and Container+SGX, the left panel reports
closed-loop maximum throughput at saturation versus `1, 5, 10, 20, 30, 40, 50`
persistent clients. The right panel reports mean latency at a fixed aggregate
10 requests/s. The fixed aggregate load isolates connection/worker-count
overhead instead of increasing load with client count. These panels answer
different questions and should be described separately.

## 5. Paper Tables

| Table | Contents and treatment |
|---|---|
| T1 `T1-selected-load-resources.tex` | Mean/p95 middlebox CPU and median/peak memory for one persistent client at 10 and 100 requests/s. Direct is N/A. |
| T2 `T2-clients-memory.tex` | p99-filtered mean CPU and p99 middlebox memory at aggregate 10 requests/s for `1, 10, 30, 50` persistent clients. |
| T3 `T3-component-costs.tex` | Mean request/response validation, quote generation/verification, DC generation, worker creation, and SGX process-startup costs. Each run contributes one mean and multiple runs produce a 95% confidence interval; missing measurements remain `--`. |
| T4 `T4-handshake-capacity.tex` | Fixed-10-concurrency full/resumed handshake capacity. Reports the highest all-runs-usable rate and the next failed tested rate as a bracket, achieved handshakes/s, mean handshake latency, and run count. |

T4's `clients=10` means a 10-request in-flight limit for fresh mode and 10 real
logical session caches for resumption. The companion
`handshake-capacity-summary.csv` preserves every tested rate and quality reason.

## 6. Analytical Outputs

The following support diagnosis and operating-point selection but are not
primary paper figures:

- `e2e_timeseries_*.pdf`: successful request dots, a trailing one-second moving
  average, overall steady mean, and failure/drop annotations. The first second
  uses the samples available since time zero; the final value uses the preceding
  one-second window. Dots are downsampled to 2000/subplot, while averages use
  all samples.
- `analysis-request-offered-achieved.pdf`: verifies whether offered request load
  was sustained.
- `analysis-handshake-offered-achieved.pdf` and
  `analysis-handshake-latency-vs-offered.pdf`: support the T4 capacity bracket.
- `analysis-latency-vs-offered.pdf`: p99 diagnostic latency, retained even
  though paper P5 uses the mean.
- `analysis-cpu-vs-offered.pdf`: diagnostic middlebox CPU curve.
- `analysis-scalability-*.pdf`: per-strategy rate/client detail.
- `cpu_timeseries.pdf` and `memory_timeseries.pdf`: resource-sampling checks.
- `e2e_latency_samples.csv` and `run_summary.csv`: normalized packet/run data
  from which figure decisions can be audited.

Paper-numbered figures/tables have complete-matrix guards. A smoke or failed
partial campaign may still produce analytical files but will not silently
produce a plausible-looking incomplete paper result.

## 7. Reproduction Checklist

Before a final campaign:

1. Record `lscpu`, `uname -a`, memory size, runtime versions, Gramine revision,
   custom Go/toolchain revision, DCAP/AESM/PCCS versions, and Git commit.
2. Enable and record CPU performance mode.
3. Build all trace-enabled artifacts and the startup-only standard SGX image:

   ```bash
   cd /home/bonsai/dcmb/DC/Middlebox
   BUILD_STARTUP_IMAGE=1 ./compile.sh
   ```

4. Run the five canonical campaign YAMLs listed in
   `PAPER_FIGURES_MEMO.md`.
5. Keep failed runs; inspect controller summary and plot quality messages rather
   than deleting evidence.
6. Use at least 5 independent runs/normal point, 10 for noisy capacity points,
   and 20-30 startup repetitions.
7. Plot each campaign independently with:

   ```bash
   python3 benchmarking/plot_latency.py experiments/<campaign-directory>
   ```

8. Archive the copied campaign YAML and generated `run_summary.csv` alongside
   the paper artifact so the plotted operating point remains reproducible.
