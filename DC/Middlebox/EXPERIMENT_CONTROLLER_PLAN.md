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
- Docker worker startup: use gateway/container trace events, especially container create to ready

## CPU And Resource Monitoring

For local deployments:

- Use `psutil` for direct child processes.
- For Docker/Podman mode, monitor the runtime machinery as a whole:
  - gateway container CPU/memory
  - optionally total CPU/memory of all containers matching `dcmb_gateway` and `dcmb-worker-*`
- Current first-pass Docker resource monitoring uses `docker stats`, which can be heavy and has already timed out under load. If Docker runs look slower or noisier than expected, replace this with lighter direct accounting:
  - whole-machine `/proc/stat` for total system CPU
  - Docker/containerd/shim process stats via `/proc/<pid>/stat`
  - optionally container cgroup files such as `cpu.stat`, `memory.current`, and `pids.current`

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
14. Copy remote logs/traces back.
15. Convert all `.bin` traces in the run folder to `.csv`.
16. Sleep `cooldown_s`.
17. Append basic run metadata/status to `summary.csv`.

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
3. Local CPU monitoring. Done for direct processes, plus Docker stats for gateway/worker machinery.
4. Trace path management and automatic bin-to-CSV conversion. Done for local runs.
5. Cleanup/cooldown between runs. Done for first pass.

Then add:

1. Direct client-to-server baseline deployment.
2. Gramine SGX deployment with full-validation and empty-handler manifests. Done for local first pass.
3. Certserver-side quote verification in the Go certserver. Done with the DCAP cgo verifier path.
4. Remote SSH wrapper and log copy.
5. Podman runtime adapter.
6. CSV merge/duration analysis helpers.
