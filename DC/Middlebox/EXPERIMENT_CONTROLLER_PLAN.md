# Experiment Controller Plan

Goal: build a Python experiment controller that starts/stops all roles, chooses parameters from YAML, stores logs/traces in structured run folders, supports local and SSH deployments, and converts trace binaries to CSV at the end of every run.

## Output Layout

Each controller invocation creates one date-time campaign folder:

```text
experiments/
  2026-06-25_18-30-12/
    campaign.yml
    campaign.log
    summary.csv
    baremetal_clients1_fresh_rate10_run1/
      metadata.json
      stdout/
      stderr/
      traces/
      csv/
      cpu/
    sgx_clients1_fresh_rate10_run1/
    docker_gateway_clients10_persistent_rate0_run1/
```

Each run folder should include exact commands, env vars, hosts, return codes, process IDs if known, stdout/stderr logs, raw `.bin` traces, converted `.csv` traces, and CPU/resource samples.

## YAML Configuration

The YAML file should own all experiment parameters and host/IP placement.

Suggested top-level shape:

```yaml
campaign:
  name: middlebox_scalability
  output_root: experiments
  runs: 3
  duration_s: 30
  warmup_s: 1
  cooldown_s: 2

hosts:
  controller:
    host: localhost
    work_dir: /home/bonsai/dcmb/DC/Middlebox
  client:
    host: localhost
    ip: 127.0.0.1
    work_dir: /home/bonsai/dcmb/DC/Middlebox
  middlebox:
    host: localhost
    ip: 127.0.0.1
    work_dir: /home/bonsai/dcmb/DC/Middlebox
  server:
    host: localhost
    ip: 10.79.1.175
    work_dir: /home/bonsai/dcmb

server:
  target_url: https://10.79.1.175:8000
  cert_url: http://10.79.1.175:5000
  request_path: /function/init

client_matrix:
  modes: [fresh, persistent]
  clients: [1, 5, 10]
  rates: [10, 100, 1000]

deployments:
  - name: direct
    kind: direct
  - name: baremetal
    kind: baremetal
  - name: sgx_full
    kind: gramine_sgx
    command: ["gramine-sgx", "middlebox"]
  - name: sgx_empty
    kind: gramine_sgx
    command: ["gramine-sgx", "middlebox_emptyhandler"]
  - name: docker_gateway
    kind: docker_gateway
    runtime: docker
    min_ready: 10
    scale_up_by: 10
    worker_trace_enabled: true
```

For distributed runs, `host` becomes the SSH target and `ip` is the address other machines should use.

## Roles

The controller should understand these roles:

- request server, listening on `:8000`
- certserver, listening on `:5000`
- deployment under test:
  - direct client-to-server baseline, with no middlebox process
  - bare metal `./middlebox`
  - Gramine/SGX middlebox, full validation and empty-handler variants
  - Docker/Podman gateway plus worker containers
- client load generator
- CPU/resource monitor

The request server is always started for each run. The certserver is started for any deployment that uses a middlebox, but it is skipped for direct client-to-server baseline runs because no delegated credentials are fetched. When both are used, the request server and certserver must be placed on the same configured server node.

## Deployment Strategies

### Direct Client-To-Server Baseline

Do not start any middlebox, gateway, or certserver process.

The client connects directly to:

```text
<server.target_url><server.request_path>
```

The client should still use the same experiment parameters as middlebox runs: fresh or persistent connection mode, number of logical clients, total rate, headers, request body, and SNI/servername. This measures the request server and client overhead without the middlebox path.

### Bare Metal

Start one `./middlebox` process directly.

Pass:

```text
OPERATOR_TARGET=<server.target_url>
OPERATOR_CERT_URL=<server.cert_url>
./middlebox -trace <run>/traces/middlebox.bin
```

### Gramine SGX

Start the SGX command configured in YAML. The controller should support both full-validation and empty-handler binaries:

```text
gramine-sgx middlebox
gramine-sgx middlebox_emptyhandler
```

Trace path should still be passed as an argument if the manifest permits it.

Startup cost is measured from process spawn until the middlebox prints a stable ready line.

The build should generate and sign separate manifests:

```text
middlebox.manifest.template              -> middlebox.manifest              -> middlebox.manifest.sgx
middlebox_emptyhandler.manifest.template -> middlebox_emptyhandler.manifest -> middlebox_emptyhandler.manifest.sgx
```

The empty-handler manifest should be equivalent to the full manifest except for the executable and trusted binary.

Manifest checklist:

- allow command-line arguments so the controller can pass `-trace`, log flags, and reuse flags
- pass through `OPERATOR_TARGET` and `OPERATOR_CERT_URL`
- pass through `MBX_EMIT_QUOTE` for attestation-enabled runs
- make the configured trace output path writable from inside Gramine
- make `schemas/` readable for the full-validation binary
- make the CA/certificate files used by the middlebox readable
- keep `/dev/attestation/*` available when `MBX_EMIT_QUOTE=1`

Attestation should be an explicit deployment/env choice. Non-SGX bare metal should not set `MBX_EMIT_QUOTE=1`, because `/dev/attestation` is not expected to exist.

### Docker/Podman Gateway

Start a fresh gateway container for every run.

Docker example:

```bash
docker run --rm --name dcmb_gateway \
  --network dcmb-middlebox-net \
  -p 9443:9443 -p 8088:8088 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v <run>/traces:/trace \
  -e GATEWAY_BACKEND_MODE=docker \
  -e OPERATOR_TARGET=<server.target_url> \
  -e OPERATOR_CERT_URL=<server.cert_url> \
  -e DOCKER_MIN_READY_OPERATORS=<min_ready> \
  -e DOCKER_SCALE_UP_BY=<scale_up_by> \
  -e DOCKER_WORKER_TRACE_ENABLED=<true|false> \
  -e DOCKER_WORKER_TRACE_HOST_DIR=<absolute run traces dir on docker host> \
  -e DOCKER_WORKER_TRACE_CONTAINER_DIR=/trace \
  dcmb_gateway:docker \
  -trace /trace/gateway.bin
```

Podman should be supported by abstracting the container runtime command and socket/mount details behind a small runtime adapter. Start with Docker, but avoid hard-coding assumptions that make Podman painful later.

For resource usage, measure the whole gateway/container machinery rather than per-worker CPU. Per-worker traces can still be collected when latency decomposition is needed.

## Readiness And Startup Cost

Add or rely on stable ready lines:

```text
[OPERATOR_READY] listening=:8443 mode=warm id=operator
[GATEWAY_READY] listening=:9443 mode=docker
```

The controller should wait for these terminal output lines. Avoid port polling because it adds measurement jitter and can hide startup timing details.

Startup measurements:

- bare metal process startup: spawn timestamp to `[OPERATOR_READY]`
- SGX startup: spawn timestamp to `[OPERATOR_READY]`
- gateway startup: spawn timestamp to `[GATEWAY_READY]`
- Docker usable startup: gateway spawn timestamp to the first `docker worker ready` log line
- Docker worker startup: parse each gateway `docker worker ready ... startup_ms=...` line

## CPU And Resource Monitoring

For local deployments:

- Use `psutil` for direct child processes.
- Write process user/system CPU time plus memory fields:
  - `rss_bytes`
  - `num_threads`
  - process-tree totals: `rss_tree_bytes`, `num_threads_tree`, and `children_count`
- The process-tree totals matter for wrapper-style deployments such as Gramine.
- Gramine runs all threads of one Gramine process in the same enclave and host
  process. Host process CPU time therefore includes those threads. If the
  application creates Gramine child processes, each child is a separate host
  process/enclave and its CPU time must be added recursively. Existing middlebox
  samples report `children_count=0`, but the monitor should still record
  `user_time_tree_s` and `system_time_tree_s` for correctness.
- For Docker/Podman mode, monitor the middlebox machinery as a whole:
  - gateway container CPU/memory
  - all worker-container CPU/memory for the current run
- Current Docker resource monitoring uses cgroup v2 directly. It refreshes the
  running container ID/name map once per second, enumerates active Docker
  cgroups at every sample for `scope: all`, reads `cpu.stat`,
  `memory.current`, and `pids.current`, and writes both per-container and
  aggregate CSVs. This avoids repeatedly spawning `docker stats` and continues
  to follow short-lived replacement workers; the slower runtime query is used
  only to attach human-readable names.

The existing sum of `docker/podman stats` values is conceptually valid when the
runtime contains only the experimental gateway and workers. Its problems are
operational and cross-runtime consistency: the controller launches `ps` and
`stats --no-stream` every sample, CLI/daemon work can perturb the benchmark,
dynamic containers can appear between discovery and sampling, and Docker and
Podman may report memory/cache with different semantics.

Current decision: cgroup v2 is available on the local Docker host and is the
default Docker collector (`source=docker_cgroup_v2`). Cgroup v2 is unavailable
on the rootful Podman target, so Podman keeps one persistent
`podman stats --all` stream and automatically falls back to the old
`--no-stream` sampler. Verify that stream on Bovisa before final runs.

Archived alternative for a future cgroup-v2 host: start the gateway under one
run-scoped parent and make workers use the same `CgroupParent`, then sample:

```text
cpu.stat:       parent usage_usec delta / wall-time delta -> aggregate CPU %
memory.current: instantaneous parent/descendant bytes
pids.current:   instantaneous parent/descendant task count
```

The parent counters include descendant worker cgroups, continue to exist while
short-lived workers are created and removed, and avoid both discovery races and
double-counting. Compute aggregate CPU percentage as:

```text
100 * delta(cpu.stat.usage_usec) / delta(wall_time_usec)
```

Read `memory.current` and `pids.current` as instantaneous values. This collector
uses ordinary file reads and does not invoke the runtime CLI at every sample.

Future cgroup-v2 implementation notes:

- create a unique cgroup parent per run;
- pass it to the gateway container (`--cgroup-parent`);
- add `CgroupParent` to the gateway's worker `HostConfig` so all workers are
  descendants of the same parent;
- locate the resulting host path once, accounting for the active systemd or
  cgroupfs driver;
- record cumulative CPU, cgroup memory, and task count at the existing
  sample interval;
- remove the parent only after the gateway and workers are gone and the final
  sample is written;
- retain the current stats collector as a temporary fallback for unsupported
  or rootless configurations.

This measures the requested gateway plus worker machinery. It deliberately does
not include `dockerd`, the Podman API service, `conmon`, AESM, or PCCS. Those are
host services and should be added only if the paper makes a broader whole-runtime
or whole-host claim.

For remote deployments:

- Start a small remote wrapper/monitor script over SSH.
- The wrapper starts the target command, tracks CPU/memory locally on that host, writes CPU CSV and stdout/stderr into the remote run folder, and exits when the process exits or receives a stop signal.
- At the end of each run, the controller copies the remote run folder back.

This is cleaner than trying to monitor remote processes from the controller via local `psutil`.

## Run Lifecycle

For each matrix combination:

1. Create run directory locally and remotely if needed.
2. Write `metadata.json` with parameters, hosts, paths, commands, and timestamps.
3. Cleanup leftovers from previous runs:
   - stop/remove old gateway container
   - remove old `dcmb-worker-*` containers from the previous run
   - clear or recreate trace directories
4. Start request server on the configured server node.
   - Start certserver too for middlebox/gateway/SGX deployments.
   - Skip certserver for direct client-to-server baseline deployments.
5. Start deployment under test.
6. Wait for readiness.
7. Sleep `warmup_s`.
8. Start resource monitors.
9. Run client for fixed `duration_s`.
10. Stop client if still running.
11. Stop deployment under test.
12. Stop servers if this run owns them.
13. Wait for trace flush.
14. Write `csv/startup.csv` with process-ready and Docker worker-ready startup measurements.
15. Copy remote logs/traces back.
16. Convert all `.bin` traces in the run folder to `.csv`.
17. Sleep `cooldown_s`.
18. Append basic run metadata/status to `summary.csv`.

Final paper campaign comments next to `campaign.runs` should state the intended
repetition policy: at least 5 runs per normal point, 10 for noisy p99 points,
and 20-30 starts per startup-only category. The checked-in example values may
remain small for smoke tests.

Metrics such as errors, saturation, latency breakdowns, and detailed throughput can be derived later from client output and trace CSVs. The controller should mainly preserve raw data reliably.

## Trace Conversion

After every run, convert all binary traces found under the run folder:

```bash
go run ./cmd/tracecsv -in <file>.bin -out <file>.csv
```

Later, add a merge step that timestamp-sorts multiple CSV files into one run-level CSV.

## Distributed Runs

The same YAML should support single-machine and three-machine layouts.

The controller should:

- create remote run directories over SSH
- launch commands remotely
- redirect stdout/stderr remotely
- run the remote CPU/resource monitor wrapper
- copy remote run directories back at the end of each run

Clock synchronization matters for cross-machine trace correlation. Keep a note in run metadata about whether NTP/PTP was verified. Client-measured end-to-end latency is still valid without synchronized clocks, but cross-host component deltas are not.

## Initial Implementation Scope

Start small:

1. Local-only controller with YAML matrix. Done in `benchmarking/run.py`.
2. Bare metal and Docker gateway deployments. Done for first pass.
3. Local CPU monitoring. Done for direct processes, Docker cgroup-v2 accounting
   for gateway/worker machinery, and a Podman stats-stream fallback.
4. Trace path management and automatic bin-to-CSV conversion. Done for local runs.
5. Cleanup/cooldown between runs. Done for first pass.

Then add:

1. Direct client-to-server baseline deployment. Done for local first pass.
2. Gramine SGX deployment with full-validation and empty-handler manifests. Done for local first pass.
3. Certserver-side quote verification in the Go certserver. Done with the DCAP cgo verifier path.
4. Process memory collection with RSS/thread fields and process-tree totals. Done for local first pass.
5. Normal-campaign startup CSV for process readiness, gateway readiness, first Docker worker readiness, and per-worker internal startup. Done for local first pass.

## Remaining Final-Experiment Work

1. Done locally: Docker uses direct cgroup-v2 samples and records
   `source=docker_cgroup_v2` in `metadata.json`. Podman uses one persistent
   `podman stats --all` stream, with the existing `--no-stream` sampler as its
   fallback. Before Bovisa paper runs, verify that rootful Podman reports
   `source=podman_stats_stream` and that newly created workers appear.
2. Keep multi-machine orchestration and clock-synchronization validation as a
   separate pass.
3. Done: additive client/gateway/worker connection bindings and per-delegation
   IDs are emitted without changing old event codes. Request-server tracing is
   deliberately deferred as a final touch; until then its cost stays in the
   dissection residual. Also recheck direct persistent connection reuse against
   the final application server before the paper campaign.
4. Done: `campaign.startup_only` skips servers, monitors, and client traffic;
   `configs_startup.yml` measures native, Gramine, SGX-Go, container, and both
   container+SGX startup paths. `dcmiddlebox-worker:sgx-standard-startup` is a
   startup-only image enabled with `BUILD_STARTUP_IMAGE=1` and must not be used
   for runtime campaigns.
5. Done for the current local format: the controller already preserves run
   status/client return codes and the client/gateway logs contain late slots,
   non-2xx responses, transport errors, and drops. The plotting normalizer now
   consumes these sources and marks affected points without requiring a
   `run.py` rewrite.

Prepared paper campaign files:

- `benchmarking/configs.yml`: single client, offered-rate sweep, full strategy
  and ablation matrix, including container resumption.
- `benchmarking/configs_clients_scalability.yml`: persistent/resumption client
  count and offered-rate sweep across representative strategies.
- `benchmarking/configs_startup.yml`: startup-only campaign, currently one
  smoke start/category; raise it to 20-30 starts/category for paper results.

## Local Command Sequence

Build all runtime artifacts, including the startup-only Gramine image:

```bash
cd /home/bonsai/dcmb/DC/Middlebox
BUILD_STARTUP_IMAGE=1 ./compile.sh
```

Quick integration passes (one short rate point plus focused resumption/SGX
container checks):

```bash
python3 benchmarking/run.py -c benchmarking/configs_all_deployments_rate10_15s.yml
python3 benchmarking/run.py -c benchmarking/configs_docker_resumption_smoke.yml
python3 benchmarking/run.py -c benchmarking/configs_docker_sgxgo_smoke.yml
python3 benchmarking/run.py -c benchmarking/configs_docker_sgxgo_trace.yml
python3 benchmarking/run.py -c benchmarking/configs_startup.yml
```

Focused reruns for the repaired resource/startup paths:

```bash
python3 benchmarking/run.py -c benchmarking/configs_docker_stats_smoke.yml
python3 benchmarking/run.py -c benchmarking/configs_startup_problematic.yml
```

Final local campaigns:

```bash
python3 benchmarking/run.py -c benchmarking/configs.yml
python3 benchmarking/run.py -c benchmarking/configs_clients_scalability.yml
python3 benchmarking/run.py -c benchmarking/configs_startup.yml
```

Plot any completed campaign with:

```bash
python3 benchmarking/plot_latency.py experiments/<campaign-directory>
```

## Next: Docker Workers Running SGX-Go Middleboxes

Add a new Docker gateway deployment strategy where every worker container runs
`middlebox_sgxgo` inside Gramine SGX. This is a whole-gateway mode: a given
gateway run uses either plain worker containers or SGX-Go worker containers, not
a mix of both.

### Current State

- `dcmiddlebox-worker:sgxgo` exists as a dedicated worker image.
- The image uses `gramineproject/gramine:latest`.
- The image copies `middlebox_sgxgo`, schemas, certs, and `jwks.dat`.
- The image generates and signs its Gramine manifest during Docker build.
- The container manifest keeps `sgx.enclave_size = "64M"`.
- A manual container run works when the host SGX devices and AESM socket are
  mounted.
- The gateway still uses TCP readiness on worker port `8443`, which is
  compatible with the SGX-Go worker.

### Gateway Docker Backend Changes

Extend the gateway's minimal Docker Engine API structs. Docker Engine already
supports device mappings through `HostConfig.Devices`; the current local Go
struct simply does not expose that field yet.

Add:

```go
type dockerDeviceMapping struct {
    PathOnHost        string `json:"PathOnHost,omitempty"`
    PathInContainer   string `json:"PathInContainer,omitempty"`
    CgroupPermissions string `json:"CgroupPermissions,omitempty"`
}
```

and add to `dockerHostConfig`:

```go
Devices []dockerDeviceMapping `json:"Devices,omitempty"`
```

Add Docker backend config fields read from env:

- `DOCKER_WORKER_SGX_ENABLED`, default `false`
- `DOCKER_WORKER_SGX_ENCLAVE_DEVICE`, default `/dev/sgx_enclave`
- `DOCKER_WORKER_SGX_PROVISION_DEVICE`, default `/dev/sgx_provision`
- `DOCKER_WORKER_AESM_DIR`, default `/var/run/aesmd`
- `DOCKER_WORKER_EMIT_QUOTE`, default empty/`0`

When `DOCKER_WORKER_SGX_ENABLED=true`, every worker created by that gateway run
should receive:

- SGX device mappings:
  - host `/dev/sgx_enclave` to container `/dev/sgx_enclave`
  - host `/dev/sgx_provision` to container `/dev/sgx_provision`
- AESM bind mount:
  - `/var/run/aesmd:/var/run/aesmd`

The device paths and AESM directory remain configurable for machines with
different host layouts. If a configured SGX device path is empty, skip that
device mapping rather than failing during config parsing; Docker will still fail
at container create/start if a non-empty configured path does not exist.

Add worker env forwarding:

```text
MBX_EMIT_QUOTE=<DOCKER_WORKER_EMIT_QUOTE>
```

only when the value is explicitly configured or useful for the SGX worker. This
allows experiments with quote emission enabled or disabled on different
machines.

Keep the existing 5s worker readiness timeout for now.

### Benchmark Controller Changes

For `kind: docker_gateway`, allow YAML keys:

```yaml
worker_sgx_enabled: true
worker_emit_quote: "1"
worker_sgx_enclave_device: /dev/sgx_enclave
worker_sgx_provision_device: /dev/sgx_provision
worker_aesm_dir: /var/run/aesmd
```

Translate these into gateway container env vars:

```text
DOCKER_WORKER_SGX_ENABLED=true
DOCKER_WORKER_EMIT_QUOTE=1
DOCKER_WORKER_SGX_ENCLAVE_DEVICE=/dev/sgx_enclave
DOCKER_WORKER_SGX_PROVISION_DEVICE=/dev/sgx_provision
DOCKER_WORKER_AESM_DIR=/var/run/aesmd
```

The deployment entry should select the SGX worker image explicitly. Runtime
campaigns use the quote-enabled form consistently; quote-disabled SGX-Go is not
a separate paper strategy because quote cost is extracted from trace events.

```yaml
- name: docker_sgxgo_full_noreuse
  kind: docker_gateway
  worker_image: dcmiddlebox-worker:sgxgo
  worker_sgx_enabled: true
  worker_emit_quote: "1"
  worker_reuse_dc: false
  min_ready: 10
  scale_up_by: 10
  container_stats_scope: all
```

Per-worker traces stay disabled in throughput and scalability campaigns. Use
`configs_docker_sgxgo_trace.yml` for a one-worker controlled trace that captures
quote generation and verification without producing a trace file per worker in
the large matrix.

### Validation Steps

1. Rebuild the gateway after Docker backend changes.
2. Start the gateway manually with:
   - `DOCKER_WORKER_IMAGE=dcmiddlebox-worker:sgxgo`
   - `DOCKER_WORKER_SGX_ENABLED=true`
   - `DOCKER_WORKER_EMIT_QUOTE=1` or `0`
3. Confirm gateway logs show `docker worker ready ... startup_ms=...`.
4. Send one client request through `https://localhost:9443/function/init`.
5. Confirm certserver quote verification occurs only when
   `DOCKER_WORKER_EMIT_QUOTE=1`.
6. Confirm gateway shutdown removes SGX worker containers; shutdown may still be
   noisy because Gramine worker containers do not always terminate gracefully.

## Deferred Memo

- Remote SSH wrapper and log copy for multi-node experiments.
- Clock synchronization metadata/checks for cross-node trace correlation.
- Startup-only campaign is implemented in `configs_startup.yml`; only a
  parallel-container startup study remains optional.
- Instrument the current Python request server with the same event names and
  `X-Trace-ID` first. Do not rewrite it in Go solely for tracing; reconsider a
  Go rewrite only if server-side overhead becomes a measured bottleneck or a
  common binary writer is worth the maintenance change.
- Throughput/resource plotting: offered vs achieved throughput, saturation/errors, CPU and memory over time, and CPU/memory versus offered rate.
- Verify the implemented rootful Podman stats stream on the Bovisa Podman
  version and retain the recorded fallback source in paper artifacts.
- CSV merge/duration analysis helpers.

## Final Campaign Memo

- Normal strategy/rate plots filter to `clients=1`.
- Scalability campaigns preserve client count as an analysis dimension.
- Resumption is a container-strategy experiment in the current design; do not
  require direct, bare-metal, or shared-Gramine resumption runs.
- Use 5 independent runs per throughput point, increasing to 10 for unstable
  p99 results; use 20-30 starts per startup category.
- The current first-pass files intentionally use one 30-second repetition. The
  single-client matrix uses rates `[1, 5, 10, 50, 100, 500, 1000]` for fresh
  connections and `[1, 10, 100, 1000, 5000]` for persistent/resumption. The
  scalability matrix uses clients `[1, 5, 10, 50]`.
- Standalone/shared `middlebox_sgxgo` is excluded from runtime and startup
  campaigns. SGX-Go is evaluated only as the worker inside the container
  strategy; shared Gramine uses the standard `middlebox` binary.
- Docker+SGX-Go runtime deployments always set `worker_emit_quote: "1"`.
- Rotate experiment order between repetitions to reduce thermal/cache/time drift.
- Keep controller-failed runs and traces for diagnostics, but mark them in
  metadata so aggregate analysis can exclude partial windows.
- Remote SSH execution, per-node resource collection, result copy, and clock
  synchronization verification remain required for the final multi-node pass.

## Runtime Robustness Status (2026-07-29)

Implemented after the first paper campaigns:

- Client HTTP operations have a fixed 5-second deadline. Fresh-mode scheduling
  still uses the 1024-request in-flight bound, but an overloaded run can now
  drain and terminate instead of retaining requests indefinitely.
- Persistent/resumption staggered clients stop waiting when the measurement
  deadline arrives. At low offered rates, fewer logical clients may
  legitimately participate because the run contains fewer request slots than
  configured clients; this is recorded rather than hidden.
- Client reports now record timeouts, scheduled/offered ratio, participating
  clients, estimated clients required at measured p99, peak in-flight work, and
  machine-readable quality flags.
- The controller marks nonzero client exits and early middlebox/gateway exits
  as failed, with `failure_reason` in campaign summary and run metadata.
- Docker campaigns with `prewarm_for_clients: true` wait for an explicit
  `[GATEWAY_POOL_READY]` line. Initial/max pool sizes are one worker for fresh,
  `clients + 1` for persistent (the extra worker absorbs the isolated client
  warmup), and `clients` for resumption.
- Performance configs use `-log_level error -minimal_logs=true`. Readiness,
  startup/listener failures, validation initialization/failures, delegation
  prefetch failures, upstream proxy failures, trace startup, shutdown receipt,
  and shutdown failures remain visible through unconditional stderr/log calls.

Focused diagnostic reruns are available as:

```text
benchmarking/configs_rerun_client_deadline.yml
benchmarking/configs_rerun_sgx_limits.yml
benchmarking/configs_rerun_container_prewarm.yml
benchmarking/configs_rerun_validation_logging.yml
```
