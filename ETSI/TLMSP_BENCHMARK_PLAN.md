# TLMSP Benchmark Plan

## Goal And Status

Maintain a reproducible bare-metal TLMSP deployment and produce standalone raw
data for comparison with DCMB. The runtime profiles, timing instrumentation,
latency runner, open-loop load generation, campaign orchestration, and
middlebox CPU accounting are implemented. DC plot integration is also
implemented through automatic discovery of copied TLMSP campaign directories.

## Deployment

```text
patched TLMSP libcurl load generator
  -> tlmsp-mb on 127.0.0.1:10001
  -> TLMSP-enabled Apache on 127.0.0.1:4444
  -> plaintext HTTP Go application server on 127.0.0.1:7000
```

The Full policy path is:

```text
tlmsp-mb -> ETSI/NewMiddlebox/client -> listener on 127.0.0.1:8080
```

The deployment is bare metal. VMs and containers are not required.

## Fixed Decisions

- Exercise only `POST /function/init` with `{"operation":"init"}`.
- Require curl success, HTTP 200, and the expected JSON status/message.
- Keep curl's complete-request-in-header-context behavior.
- Explicitly forward response-body containers unchanged.
- Keep the TLMSP read-queue idle fallback at zero for these experiments.
- Enable `TCP_NODELAY` on curl and both middlebox TCP legs.
- Use the Go application server as plaintext HTTP on `127.0.0.1:7000` while
  preserving TLS as its default for DCMB.
- Compare only Full and No-handler; do not create an empty handler.
- Use five independent 60-second fixed-rate windows, one second of component
  warmup, one
  isolated fresh warmup request, a one-second pause, and one retained-connection
  prime before persistent measurements.
- Use fresh at 1 request/s and persistent at 10 requests/s for latency.
- Start Apache with unlimited keep-alive requests so a persistent measurement
  does not acquire a new handshake after every 100 requests.
- Preserve one CSV row per completed request and all source timestamps.
- Drop the first 10 measured rows only in the later plotting adapter, matching
  current DC processing.

## Controlled Profiles

`local_init.ucl` is Full. Request and response matches invoke the external Go
validation path. Handler stderr now flows into the middlebox's stderr once per
run rather than opening `stderr.txt` once per invocation.

`local_init_no_handler.ucl` is the controlled baseline. It keeps the same
contexts, read/write permissions, regex matching, header-context rewriting,
response-body forwarding, endpoint addresses, and handshake topology. It
replaces each handler output with the in-process `${0}` matched bytes and does
not launch the listener.

Full minus No-handler is therefore named **external validation-path overhead**.
It includes process creation, pipes, serialization, local HTTP, listener work,
and validation; it is not presented as validation-function time alone.

## Timing Boundaries

Instrumentation is enabled only when `TLMSP_BENCH_TRACE` is nonempty and not
`0`. Emitted events use `CLOCK_REALTIME` Unix nanoseconds and are written to
stderr. This places client, middlebox, controller, and application-server
events and load-generation windows in one clock domain on the same host.

Client events:

```text
transfer_start   Curl_pretransfer entry
handshake_start  immediately before the first SSL_connect
handshake_done   successful completion of SSL_connect
request_start    Curl_http entry, before HTTP message construction
response_done    Curl_posttransfer after the complete response body
```

Derived values:

```text
end_to_end_ms = response_done - transfer_start
setup_ms      = handshake_done - transfer_start
handshake_ms  = handshake_done - handshake_start
request_ms    = response_done - request_start
```

`request_ms` starts at the agreed request-construction boundary. The DC plot
adapter maps it to the corresponding
`client_request_start -> client_response_done` comparison.

Middlebox handshake events:

```text
server_half_start  outbound transport connected; server-facing handshake runs
client_half_done   client-facing SSL leaves its initial handshake state
server_half_done   server-facing SSL leaves its initial handshake state
```

The two halves overlap and must not be added. The plot shows the complete
client-observed TLMSP handshake in grey. The half durations are for text only.

Middlebox external-handler events:

```text
request_handler_start / request_handler_done
response_handler_start / response_handler_done
```

These surround the generic pipe/fork/handler boundary, so the CSV can report
request and response validation-path costs separately.

The traced Go application server contributes
`requestserver_request_start` and `requestserver_response_start`. Together,
the client, middlebox, and application events divide persistent `request_ms`
into four grey protocol/path intervals, request validation, application
processing, and response validation. `dissection_ok=1` records that all eight
boundaries were present and ordered. Full minus No-handler remains an aggregate
external-validation-path cross-check rather than the directional split used in
the bar.

## Load And Campaign Design

`tlmsp_loadgen` is the sole native benchmark driver:

- Persistent mode reuses one easy handle and one serial HTTP/1.1 connection.
- Fresh mode creates a new easy handle and connection for every request.
- Fresh duration mode uses `curl_multi`, allowing handshakes to overlap up to
  `--max-in-flight`.
- Count mode supports short manual checks.
- Duration mode schedules fixed-rate open-loop slots. Missed slots are counted
  and never replayed as a catch-up burst.

`run_latency.py` runs the driver, parses client events/results, merges
middlebox events by client port and transfer ordinal, and writes per-request
CSV rows.

`run_campaign.py` starts and stops all local components for every point, runs
the isolated warmup, executes the measured window, and writes a transferable
campaign directory. It supports:

- `latency`: Full and No-handler, fresh 1/s and persistent 10/s.
- `throughput`: single-connection persistent open-loop rate sweep.
- `handshake`: concurrent fresh-handshake sweep, No-handler only and no
  listener.

The controller refuses occupied ports instead of terminating existing user
processes.

## CPU Scope And Validity

The controller samples `/proc/<tlmsp-mb pid>/stat`. It uses the middlebox's
user/system time plus reaped child user/system time, which captures short-lived
shell, `stdbuf`, and `client` processes without racing process discovery.
The Go listener is deliberately excluded. Report this as **TLMSP middlebox
process-tree CPU**, not total TLMSP policy-architecture CPU.

A rate point is valid when there are no request errors, every launched request
completes, and the scheduled-success deficit is no more than the larger of one
boundary slot or 2 percent. The throughput table should select the highest
valid rate for the Full profile. Handshake capacity uses only No-handler.

## Repository Ownership

- `tlmsp-tools`: `davideandreotti/tlmsp-tools` is `origin`;
  `ricnava00/tlmsp-tools` is read-only history in `upstream`.
- `tlmsp-curl`: `davideandreotti/tlmsp-curl` is `origin`;
  `ricnava00/tlmsp-curl` is `upstream`.
- Parent submodule URLs point to the owned forks.
- Curl's local `master-tlmsp` branch still needs its upstream branch repaired
  with an authenticated fetch/push before these changes can be committed and
  pinned from the parent repository.

## Verification Completed

- `tlmsp-tools` builds and installs into `/home/bonsai/dcmb/.tlmsp`.
- `tlmsp_loadgen` builds with `-Wall -Wextra -Werror`.
- Full fresh and persistent smoke requests return HTTP 200 with complete
  request/response handler timestamps.
- No-handler fresh and persistent smoke requests return HTTP 200 without the
  listener.
- One-second duration checks completed 20 fresh requests/s and 50 persistent
  requests/s with no failures or missed slots.
- A 0.2-second fresh run offered at 500/s reached seven overlapping handshakes,
  completed 33 requests, and reported 67 missed slots rather than catching up.
- The self-managed controller's occupied-port guard was exercised.
- A controller-owned campaign completed all four Full/No-handler fresh and
  persistent points, produced request/event/CPU CSVs, marked every smoke point
  valid, and cleaned up all component ports.
- A wall-clock Full validation campaign completed 12 fresh and 120 persistent
  requests with no failures; every row had a complete seven-interval request
  dissection whose components exactly reconstructed `request_ms`.
- Two-point request-throughput and concurrent fresh-handshake smoke sweeps
  completed without errors or missed slots.
- The DC plot adapter was exercised against existing paper campaigns. TLMSP is
  the bottom P1/P2 entry, the rightmost P3 entry, an optional P5a curve, a
  persistent-capacity-table row with middlebox process-tree CPU, and an
  open-loop fresh-handshake-capacity row.

## Deferred

- Run final campaigns with at least five independent repetitions where
  confidence intervals are required.
- Decide after viewing the final scale whether to retain TLMSP in P5a; the
  `INCLUDE_TLMSP_IN_P5A` plotting constant makes the line removable.
- Suppress or repair close-time TLMSP EOF diagnostics.
- Clean redundant ETSI source and legacy experiment files after final runtime
  behavior is confirmed.
