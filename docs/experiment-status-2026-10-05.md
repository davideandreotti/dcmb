# Experiment status and trace/buffer findings — 2026-10-05

Updated 2026-10-18 with the native-only clean-server handover, following the
October 12 campaign consolidation and plot generation. Successful execution does
not by itself make a dataset the preferred source for final figures. Lower
throughput alone is not a reason to invalidate or delete a result.

## Campaigns to keep

Folder names below are relative to `DC/Middlebox/experiments/`.

| Folder | Status / use |
| --- | --- |
| `2026-10-03_13-45-38_latency_dissection` | FINAL latency/violin source: 60 successful DCMB runs plus 20 TLMSP repetitions under `tlmsp/latency/`. |
| `2026-10-04_08-50-07_middlebox_startup` | Current startup source. 30 successful runs; no rerun planned. |
| `2026-10-03_17-40-34_paper_single_client` | Historical reference, unchanged: 420 successful runs. Its 85 Direct persistent runs were copied into the final single-client campaign; the other 35 low-load fresh/resumed runs remain here. |
| `2026-10-03_14-53-07_handshake_capacity` | Historical reference, unchanged: 140 controller rows successful; expected fresh-container saturation errors remain. Its 15 Direct runs were copied into the final handshake campaign. |
| `2026-10-04_02-24-19_clients_scalability` | Previous reference: 280 throughput reports, zero client errors. Includes 30 shared-SGX replacement runs merged from `2026-10-04_12-14-38_clients_scalability_shared_sgx_rerun`; do not restore the original failed points. |
| `2026-10-05_21-32-03_clients_scalability` | FINAL scalability source: 280 runs, all four strategies, closed-loop and aggregate 500 RPS. 275 strict checks passed; five sparse-EOF samples accepted with warnings for throughput and mean successful-response latency. |
| `2026-10-06_04-17-45_paper_single_client` | FINAL persistent single-client throughput source: 425 DCMB runs (340 final middlebox + 85 imported Direct) plus 130 TLMSP repetitions. Five repeats through 5000 RPS for each DCMB strategy; retain offered-load deficits as capacity evidence. |
| `2026-10-06_11-25-13_handshake_capacity` | FINAL handshake source: 140 DCMB runs (125 final middlebox + 15 imported Direct) plus 20 TLMSP repetitions. All resumed replacements passed; expected fresh-container saturation points remain. |

Completed order: scalability, single-client persistent sweep through
5000 RPS (340 runs), then handshake capacity (125 runs). Five 60-second repeats,
quotes disabled, client/service traces retained. Low-load latency and startup
are not being rerun. Do not mix old trace/GC conditions into new performance
series without labelling them. Preserve old memory measurements with their
original campaign provenance; buffer-only shared SGX skips its memory sampler.

The final campaign YAMLs and ignored targeted rerun helper
`DC/Middlebox/build/rerun_failed_shared_sgx.py` are retained at the user's request
for future reference. Its original-target checks prevent blind replay against
already repaired/accepted data. The consolidated final plots were generated
on October 12. A native-only move to a clean host is now planned below; it does
not itself replace or invalidate any existing measurements.

## Final campaign consolidation — completed 2026-10-12

The final campaigns were renamed in place, retaining their actual timestamps
and all original run folders:

| Previous folder | Current folder |
| --- | --- |
| `2026-10-05_21-32-03_clients_scalability_buffers_rate500` | `2026-10-05_21-32-03_clients_scalability` |
| `2026-10-06_04-17-45_paper_single_client_buffers` | `2026-10-06_04-17-45_paper_single_client` |
| `2026-10-06_11-25-13_handshake_capacity_buffers` | `2026-10-06_11-25-13_handshake_capacity` |

Historical sections below retain the names used when those runs were performed.
Individual point names, including their real `rate500` workload labels, were
not changed. Only the `run_dir` locators in each campaign's `summary.csv` were
updated. Each original index is backed up as `summary.before-rename-20261012.csv`.
Recorded statuses, failure reasons, per-run metadata, and executed `campaign.yml`
settings are unchanged, including their historical buffer-mode campaign names.
Each renamed campaign has a `consolidation-20261012.json` provenance record.

Only the missing Direct baselines were copied from the older DCMB campaigns:

- Single-client: 85 Direct persistent runs, 17 rates from 1 through 5000 RPS,
  five repetitions each, from `2026-10-03_17-40-34_paper_single_client`.
- Handshake: 15 Direct fresh runs at 3000, 4000, and 5000 RPS, five repetitions
  each, from `2026-10-03_14-53-07_handshake_capacity`.

Imported statuses are indexed separately in `summary-imported.csv`, which the
existing plotter combines with `summary.csv`; there are no duplicate run names.
Each destination also has `imports-20261012.json`, identifying source paths and
metadata checksums. The 35 old single-client low-load samples (five Direct fresh,
20 middlebox fresh, and ten container resumed) were not imported. Latency uses
the dedicated latency-dissection campaign instead. No superseded middlebox
throughput data replaced newer measurements. Scalability required no imports.

The TLMSP extension `ETSI/Benchmarking/results/tlmsp_extension_20261007-073818`
was merged into `tlmsp_matrix_20261006-211559`: Full throughput has 55 repetitions,
No-handler throughput 75, latency 20, and handshake 20, totaling 170 unique points.
Original throughput summaries were backed up as
`summary.before-extension-20261007-073818.csv`; original metadata was retained,
and extension metadata and merge provenance are stored alongside each summary.
All points remain, including the 30 offered-load-deficit points that do not meet
the runner's tolerance. The highest tested rates passing all five repetitions
are Full persistent 100 RPS, No-handler persistent 1500 RPS, and No-handler fresh
handshakes 125 RPS. These are benchmark offered-load boundaries, not independently
established middlebox-only limits; launched requests had zero errors.

The merged TLMSP data was copied unchanged into the DCMB plotting layout:

| TLMSP source below `tlmsp_matrix_20261006-211559/` | Destination below `DC/Middlebox/experiments/` |
| --- | --- |
| `latency/` (20 repetitions) | `2026-10-03_13-45-38_latency_dissection/tlmsp/latency/` |
| `throughput/` (130 repetitions) | `2026-10-06_04-17-45_paper_single_client/tlmsp/throughput/` |
| `handshake/` (20 repetitions) | `2026-10-06_11-25-13_handshake_capacity/tlmsp/handshake/` |

Each `tlmsp/` destination has `imports-tlmsp-20261012.json`. Every copied file
was checked against its source with SHA-256, and original campaign inventories
were checked across the renames. No plotter, runtime, build, or benchmark
configuration edits were made; no new measurements or plots were generated.

Older DCMB campaigns, their historical memory measurements, the original TLMSP
matrix, the separate TLMSP extension, and all smoke folders remain intact.
No data was deleted. Historical SGX memory was not inserted into buffer-mode runs.
The retained targeted rerun helper still references the old campaign names;
it is historical reference and must not be replayed unchanged after these renames.

## Native-only clean-server migration — prepared 2026-10-18

The user confirmed that only native Shared (`baremetal_full_reuse`) and native
Container (`docker_full_reuse`) are to be repeated. Both SGX strategies, Direct,
TLMSP, handshake capacity, startup, and the dedicated latency campaign remain
out of scope. The two matrices are separately selectable: 170 single-client
repetitions and 140 scalability repetitions. No new measurement has been run.

[The migration handover](native-experiment-handover.md) records GitHub cloning,
native-only rebuilding and Docker image creation, host/IP/certificate setup,
CPU-state handling, smoke checks, one-sudo execution, and ownership steps.
[Single-client](native-single-client.yml) and [scalability](native-scalability.yml)
templates were derived from the final campaigns' copied configs, selecting only
the two native deployments. Host paths/IP are placeholders; single-client keeps
spin pacing and scalability keeps timer pacing. All other workload/pool/tracing
settings are retained, and the existing runner is unchanged.

Publish the portable implementation and canonical settings before cloning.
The publishing worktree separates these from this host's paths, Podman socket,
Rocky library mappings, and dataset-specific plotting exceptions. Rebuild
binaries/images and regenerate certificates on the destination; do not transfer
measurements, virtual environments, or CPU restore state. The optional SGX-Go
compiler copy and ETSI submodule ownership are documented in the handover.
No destination was contacted, and no commit or push was made during preparation.
Keep new-host results separately labelled until cross-host comparability and
any final replacement are explicitly reviewed.

The October 18 review found no duplicate points in the latest plot summaries.
The large native latency CIs reflect differing new-campaign repetition means,
not accidental imports of older middlebox runs. Slow native Shared repetitions
were slower throughout their measured minute, with no measured reconnections;
their cause remains undetermined. No filtering or benchmark-code fix was made.

## Portable publication preparation — 2026-10-18

Prepared branch `publish/reproducible-experiments` in a separate worktree;
`main`, the current `local/dcmb-host` checkout, and experiment data were not
switched, committed, pushed, or rewritten. The portable copy includes:

- Connection-specific response-buffer reuse and buffer-only tracing, gateway
  buffer-mode forwarding, and focused tests.
- Canonical final workload settings, quote generation off, and standard shared
  Gramine defaults (2G, 256 threads, GOMAXPROCS=32, GOMEMLIMIT=1300MiB).
- The existing opt-in EPC page-in probe fix, EPC occupancy extraction, and
  SGX1/SGX2 comparisons gated on data for both platforms.
- The CPU performance script and updated README/handover/native Docker templates.

Host paths, Podman settings, Rocky library mounts, client CA/compiler default
path edits, ETSI local path edits, sparse-EOF acceptance, and the historical
SGX2 per-client-rate plotting reinterpretation remain outside the publishable
changes. Published canonical YAMLs use path/IP placeholders and Docker.
Scalability, handshake, latency, and startup settings were compared with the
actual final campaigns after normalizing host adaptations. Single-client keeps
the same 425-point persistent matrix plus its existing 35 low-load reference
repetitions; the native-only handover selects 170 single-client and 140
scalability repetitions without those extras.

Validation: nine generic plotting regression tests pass, including single-
platform/no-overlay and matched-platform EPC checks. Focused trace-enabled Go
tests pass for trace, gateway, and middlebox; no-trace package tests also pass.
The middlebox tests required local socket access outside the sandbox, not SGX
or containers. All seven YAML matrix counts and handover shell syntax were
checked. No new campaign, image build, or new-server test was run.

During the follow-up review on October 18, the prepared worktree was switched
to local `main` at the user's request. Its edits remain unstaged/uncommitted;
the branch tip is unchanged, and nothing was pushed. The original checkout
remains on `local/dcmb-host` with its host-only adaptations intact.

## Superseded reruns — deleted 2026-10-06

These two shared-only reruns completed without client errors, but used the
earlier implementation without retained logger buffers. They are superseded
for final throughput figures, not failed experiments.

Former location: `DC/Middlebox/build/superseded-reruns.siyrTi/`.

- `2026-10-04_18-07-43_paper_single_client_shared_no_middlebox_trace`: 180 runs.
- `2026-10-04_21-55-26_handshake_capacity_shared_no_middlebox_trace`: 40 runs.

Both raw folders were permanently removed with the user's approval after final
replacement acceptance. They had been explicitly marked TO DELETE after
validation; this was not deletion merely because their throughput was lower.

Also permanently removed:
`DC/Middlebox/experiments/2026-10-04_22-43-05_clients_scalability_no_middlebox_trace_rate500`
(210 runs). This mixed folder had previously been marked KEEP UNTIL VALIDATION,
not TO DELETE: its 70 container 500-RPS points were then unique. On 2026-10-06
all 210 names were confirmed present in the 280-run final buffer campaign,
including the two fixed-rate EOF samples now accepted with warnings. The old
mixed campaign had no internal SGX memory-sampler files. Historical internal
SGX memory data remains in the untouched
`2026-10-04_02-24-19_clients_scalability` campaign (140 memory CSV/summary files).

Total allocated data removed across these three campaigns: approximately
85.6 GiB. No raw backup was created. Older main campaigns, all three final
buffer campaigns, final YAMLs, and the rerun helper were preserved.

## July 2026 campaigns — superseded

Reviewed on 2026-10-06. One additional historical comparability lesson was
confirmed: July used the retired Python application server
`PerformanceMeasuring/certs_server.py`, while the October canonical campaigns
use the Go `./appserver`. The certificate service was already `./certserver`;
this was an application-server change, not a switch from a Python certificate
service. July configured one repetition per point, rather than the current
five.

Evidence: copied configs/metadata from
`2026-07-08_17-19-08_huge_comparison_full`,
`2026-07-08_09-36-25_clients_scalability`, and
`2026-07-09_20-49-35_docker_resumption_rootful`.
Their absolute throughput/latency values are historical end-to-end results,
not interchangeable current middlebox baselines. A July/October difference
cannot be attributed solely to a middlebox edit; this review did not establish
that the Python server was the bottleneck.

No additional validated tuning or performance conclusion is retained from
July. Handler ablation, credential reuse/resumption, container readiness and
resource-collection lessons are already covered by the current methods and
later campaigns; their older forms are not repeated here. July raw folders
were subsequently permanently deleted on 2026-10-06 with the user's approval:
all 13 `2026-07-*` campaign folders, approximately 26.1 GiB of run data.
No raw-data backup was created; the historical findings above remain in this
memo. September and October campaigns, including both rate500 folders and the
new buffer-mode runs, were preserved.

## Shared-SGX startup replacements — requested 2026-10-06

The completed buffer campaigns had four shared-SGX startup failures: two in
scalability (278/280 controller rows ok) and two in single-client (338/340 ok).
All four stopped before client launch, during schema-file loading: Go's I/O
poller pipe creation returned EINTR (error 4), and the runtime terminated rather
than retrying. The interrupting signal was not identified. No runtime, binary,
manifest, tracing, GC or workload changes were authorized for these replacements.

Only these original names are selected:

- In `2026-10-05_21-32-03_clients_scalability_buffers_rate500`:
  `sgx_full_reuse_clients1_persistent_rate500_run1` and
  `sgx_full_reuse_clients20_persistent_closed_run5`.
- In `2026-10-06_04-17-45_paper_single_client_buffers`:
  `sgx_full_reuse_clients1_persistent_rate1000_run4` and
  `sgx_full_reuse_clients1_persistent_rate1250_run3`.

Status: COMPLETED and checked on 2026-10-06. All four replacements reached
readiness, exited successfully, produced client traces, and reported zero
errors/timeouts/non-2xx responses. In the order listed above, achieved rates
were 499.9986, 10513.2916, 1000.0095, and 1249.8757 RPS. The 20-client closed-loop
replacement was consistent with its four peers (10579.7–10721.5 RPS).
The original names/iterations and complete campaign matrices were preserved;
at that check scalability had 280/280 controller rows ok and single-client 340/340.
These counts do not imply that the separate container-SGX samples are error-free.
The underlying shared-SGX startup issue was not fixed.

The ignored helper
`DC/Middlebox/build/rerun_failed_shared_sgx.py` checks all four original failures,
replaces their folders, and executes exactly one replacement attempt per name
using the original copied `campaign.yml` and repetition index. It leaves the
remaining runs untouched and updates only the selected summary rows. A failed
replacement remains marked failed; there are no automatic retries. Successful
replacement names/counts match the original campaign, but this memo retains
the startup-failure/replacement history. Container-SGX issues are out of scope
for that step. The original four startup-failure folders were replaced, not
archived; their failure history is retained here.

## Container-SGX replacements — requested 2026-10-06

Status: FIRST ATTEMPT COMPLETED on 2026-10-06: 16 passed, 14 failed validation
(scalability 12/25 passed; single-client 1/1; resumed handshake 3/4).
All 30 finished, produced client CSVs, and their 120 managed processes exited
zero. The 14 rejected samples had 21 EOFs total, no timeouts or non-2xx responses.
Thirteen were scalability samples with broadly healthy throughput; the remaining
resumed 500-RPS/run2 sample achieved 488.3004 RPS with two EOFs. Worker startup
exits appeared in 28/30 attempts, including 14 accepted samples, so that symptom
alone is not a sufficient explanation of request errors.

One attempt was authorized
for each of these 30 original repetitions, using each campaign's copied
`campaign.yml`, unchanged pool/reuse/workload settings, and original iteration.
No source, manifest, image or plotting-filter changes are part of this step.

- `2026-10-05_21-32-03_clients_scalability_buffers_rate500`: 25 repetitions of
  `docker_sgxgo_full_reuse`, persistent mode. Selected clients/load/repetitions:
  1/closed/2; 5/closed/2,4; 10/closed/4; 10/rate500/1–5;
  20/closed/1,4,5; 30/closed/3; 30/rate500/2,5;
  40/closed/2,5; 40/rate500/3–5; 50/closed/1,2,5; 50/rate500/1,3.
  These original samples contained 38 EOFs in total, 1–3 per repetition.
- `2026-10-06_04-17-45_paper_single_client_buffers`:
  `docker_sgxgo_full_reuse_clients1_persistent_rate250_run3`, one EOF,
  249.6150/250 RPS.
- `2026-10-06_11-25-13_handshake_capacity_buffers`:
  `docker_sgxgo_full_resumption_clients10_resumption_rate125_run4`
  (two EOFs, 123.8937/125 RPS), `rate250_run1` (30 errors, 13.4199/250 RPS),
  `rate500_run2` (30 errors, 19.7918/500 RPS), and `rate500_run5`
  (31 errors, 13.0142/500 RPS), all sharing the same preceding name prefix.

The collapsed 250/500-RPS points are not the expected capacity boundary:
October 3 sustained both rates with zero errors in all five repetitions,
and the new campaign sustained 750 RPS in all five. Sparse EOFs occurred during
measurement; they are not merely shutdown noise. Of the 25 affected scalability
samples, 21 also logged worker startup exits, but other samples had startup
exits without EOFs. No single cause has been established. Additional retained
buffers in a 64-MiB container enclave and gateway ticket-bound worker recovery
are hypotheses, not confirmed diagnoses.

Invoke the same ignored helper with `--container-sgx`. It verifies all 30
original targets before modifying data, then permanently removes only the
next selected folder, runs that exact repetition in place, and updates only
its summary row. No raw backup is made. Remaining folders/rows, the repaired
shared-SGX samples, and expected fresh-handshake saturation points are untouched.
A replacement with errors, missing readiness/traces, nonzero process exits,
insufficient paced throughput, gateway drops, or resumed-handshake fallbacks
is marked failed; it is not retried automatically. After execution, verify the
results before merging these buffer campaigns into the older main campaigns
or regenerating final plots. Existing plots are not refreshed by the helper.

### Second container-SGX attempt — requested 2026-10-06

Status: COMPLETED on 2026-10-06: nine passed and five failed the unchanged strict
zero-error checks. The user authorized one
additional unchanged attempt for the 14 remaining failures, not the 16 that
passed. Use the same helper with `--container-sgx-retry`:

- Scalability, `docker_sgxgo_full_reuse`, persistent mode: 10/closed/4;
  10/rate500/1,3; 20/closed/5; 30/rate500/2,5; 40/closed/2,5;
  40/rate500/3; 50/closed/2,5; 50/rate500/1,3 (13 repetitions).
- Resumed handshake: `docker_sgxgo_full_resumption_clients10_resumption_rate500_run2`.

Original names, copied configs, pool settings and quality checks are unchanged.
Only these 14 rejected folders were permanently replaced; no raw backup was
made. The first attempt's 16 passing replacements and repaired shared-SGX runs
were preserved. No automatic retry or implementation change was included.

### Final acceptance of five sparse-EOF scalability samples

The user accepted these latest repetitions for throughput and mean
successful-response latency on 2026-10-06; no further replacement is planned:

| Clients/load/repetition (`docker_sgxgo_full_reuse`, persistent) | EOFs | Achieved RPS |
| --- | ---: | ---: |
| 30/rate500/2 | 1 | 499.9807 |
| 30/rate500/5 | 1 | 499.9907 |
| 40/closed/2 | 1 | 28293.9781 |
| 40/closed/5 | 4 | 28016.4248 |
| 50/closed/5 | 5 | 28443.3557 |

All five finished with full client participation, managed process exits zero,
and no timeouts/non-2xx responses. Their EOF rates are at most 0.00334%.
The fixed-rate affected means were 0.643–0.653 ms versus 0.620–0.643 ms in
clean peers; closed-loop means were also close to peers. These comparisons
do not establish causality. Failed requests have no successful-response latency
and are not silently assigned a response time; error counts must remain visible.

This host's local plotter treats otherwise healthy persistent runs with at most five
transport errors and a failure fraction <=0.01% as warnings, not invalid points.
Nonzero client exits, non-2xx responses, timeouts, incomplete participation,
material offered-load deficits, excessive gateway drops, and resumed-handshake
failures remain invalid. A failed status is exempted only when the recorded
reason is the targeted helper's strict client/trace quality rejection and the
client exited zero; all other failed statuses remain invalid. Original summary
statuses, failure reasons, and raw error counts are not rewritten. The old
unconditional 40-client container-SGX plotting exception was removed so these
same guarded checks apply there. Acceptance is not a claim of error-free runs
or a validated explanation/fix of worker instability. This dataset-specific
waiver remains local and is not part of the portable publishing copy.

## Findings preserved before removing the probes

These were short diagnostics, not canonical campaigns. Native comparisons used
40 persistent closed-loop clients and retained client/server/certserver tracing.
Settings and instrumentation differed between diagnostic series: compare rows
within a series, not absolute throughput across series.

- TuneD `throughput-performance` did not recover the trace-off throughput.
  NUMA node-0 binding improved shared-SGX trace-off from about 5192 to 6142 RPS,
  but the unbound trace-on point reached about 9200 RPS. The NUMA campaign was
  partial (five completed runs); topology did not explain the whole gap.
- Compiling tracing out did not fix the gap: native trace-capable/recording-off
  averaged 26204.6 RPS versus 24646.2 for the no-op build; SGX 5234.9 versus
  5179.0. Two repeats per case, before the connection-buffer change.
- Profiles identified a 32 KiB reverse-proxy response-copy allocation per
  request. Reusing one response buffer per existing connection/session improved
  native trace-off from 23130.1 to 35564.1 RPS, and trace-on from 35110.1 to
  47793.9 RPS (one 20-second run per case). HTTP/1.1 sequential connection use
  is the current scope; this is not an HTTP/2 concurrent-stream design.
- GC counterfactuals support heap/GC pacing as a major cause. Native trace-off
  achieved 29808.9 RPS; retaining 5 MiB without logging achieved 42842.1;
  GOGC=200 without logging achieved 56831.7; trace-on at GOGC=100 achieved
  43657.8. One run per case; no canonical GOGC change was made.

Shared-SGX GC counterfactual (one 30-second run per case, 2 GiB enclave,
256 threads, GOMAXPROCS=32, GOMEMLIMIT=1300MiB, quotes off):

| Condition | RPS / outcome |
| --- | ---: |
| Recording off, GOGC=100 | 5139.8 |
| Recording off, GOGC=200 | 10399.6 |
| Recording on, GOGC=100 | 8999.6 |
| Recording off, GOGC=400 | Failed: runtime allocation/OOM error; no completed throughput report. |

GOGC=400 failed while reserving another runtime heap arena. Its reported
in-use bytes are not total enclave usage; the precise allocation cause remains
unresolved. Do not describe it as reaching the Go 1300 MiB soft limit or assume
GOGC=200 is safe for full campaigns. SGX trace-on also enabled its memory
sampler, unlike the native retained-memory control.

Four-way native comparison after connection-buffer reuse (two 30-second
repeats, forward/reverse order, GOGC=100, GOMAXPROCS=64):

| Middlebox condition | Mean RPS | GC cycles / 1000 requests | Heap goal MiB |
| --- | ---: | ---: | ---: |
| Recording off, no logger buffers | 36468 | 7.93 | 18.60 |
| Retained logger buffers, recording off, no writer | 56470 | 3.23 | 30.80 |
| Actual tracing to `/dev/null` | 48679 | 3.93 | 30.50 |
| Actual tracing to a file | 50328 | 3.75 | 30.64 |

Retained buffers recorded zero events and wrote no trace file. Thus disk writes
were not necessary for the speedup. Larger reachable heap and reduced GC work
support the GC-pacing explanation; involvement of particular `sync.Pool`
caches is a hypothesis, not proven attribution. File and `/dev/null` ranges
overlapped: their mean difference does not establish a filesystem-write benefit.

Final production-source native `-trace buffers` check: two 20-second repeats,
off/buffers/buffers/off order, GOGC=100, TuneD `latency-performance`, no diagnostic
overlays/profiling. Off averaged 35300.7 RPS; buffers averaged 55828.9 RPS
(+58.2%). Both wrote no middlebox trace file; all four runs had zero client
errors/timeouts/non-2xx responses and full client participation. This is short
native evidence, not proof of the same gain for every SGX/container workload.

Both buffer preflights also passed before removal. Shared SGX alone achieved
14784.3 RPS at 40 clients. The four-strategy preflight had zero client errors:
native 10478.1 RPS (2 clients), shared SGX 14826.0 (40), container 9886.5 (2),
container+SGX 4934.0 (2). Unequal client counts make these deployment checks,
not a comparative performance experiment.

Detailed diagnostic reports and build/profile artifacts remain in ignored
`DC/Middlebox/build/` directories: `performance-profile-probe`,
`connection-buffer-probe.xpO8gB`, `trace-path-comparison.SRi4uD`, and
`trace-buffers.DWh0ax`. Their original raw-campaign links will no longer resolve.

## Raw folders approved for permanent removal

All names below are under `DC/Middlebox/experiments/`. Seven folders were
permanently removed on 2026-10-05, freeing approximately 39.1 GiB. Five remain
pending sudo authentication: the four morning probes and the four-strategy
preflight. Passwordless sudo was unavailable. This memo is not a raw-data backup.

- PENDING SUDO: `2026-10-05_07-48-21_clients_scalability_throughput_performance_probe`
- PENDING SUDO: `2026-10-05_08-08-38_clients_scalability_trace_enabled_probe`
- PENDING SUDO: `2026-10-05_08-19-27_clients_scalability_sgx_numa_probe`
- PENDING SUDO: `2026-10-05_08-31-18_clients_scalability_notrace_build_probe`
- REMOVED: `2026-10-05_17-37-42_native_trace_cpu_profile_probe`
- REMOVED: `2026-10-05_17-44-10_native_trace_gc_counterfactual_probe`
- REMOVED: `2026-10-05_17-47-15_sgx_trace_gc_counterfactual_probe`
- REMOVED: `2026-10-05_19-07-38_native_connection_buffer_probe`
- REMOVED: `2026-10-05_19-28-32_native_trace_path_comparison`
- REMOVED: `2026-10-05_20-36-28_native_trace_buffers_flag_probe`
- REMOVED: `2026-10-05_21-13-44_buffers_shared_sgx_preflight`
- PENDING SUDO: `2026-10-05_21-28-16_buffers_preflight`
