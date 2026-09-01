# Trace Status And Remaining Work

Current state:

- `internal/trace` exists with build-tagged no-op/active implementations.
- `cmd/client`, `cmd/gateway`, and `cmd/middlebox` accept `-trace`, `-trace-buffer-events`, and `-trace-drop-on-full`.
- Client, gateway, Docker container lifecycle, and middlebox worker paths have trace probes.
- Successful client TLS callbacks also emit `client_tls_resumed` with argument
  `1` for a resumed handshake and `0` for a full handshake.
- Full and empty middlebox builds emit schema-compilation start/done events
  before worker readiness.
- Docker workers can write per-worker trace files when `DOCKER_WORKER_TRACE_ENABLED=true`.
- `cmd/tracecsv` converts binary trace files to CSV.
- `compile.sh` builds trace-enabled binaries with `-tags trace`.
- Client, gateway, and middlebox emit additive canonical TCP connection-key
  bindings. The middlebox emits a unique delegation ID, binds it to the worker
  connection, sends it to certserver, and certserver uses it for concurrent
  quote/DC-generation events.
- Gateway splice first-byte events and middlebox upstream/downstream boundary
  events are implemented.
- `cmd/appserver` is a separate traced Go HTTP/1.1 TLS request server. It
  records request-body receipt, response start, and response completion under
  the forwarded `X-Trace-ID` without per-request text logging.
- Each downstream middlebox TLS connection owns one eagerly established
  upstream TLS connection. `middlebox_upstream_connection_bind` links that
  native upstream-session ID to the existing connection key.

## Current Certserver Instrumentation

`cmd/certserver` now supports the common trace flags and records request,
delegated-credential generation, quote verification, response, and error events.
Legacy SNI events remain unchanged. New `*_by_id` events use the unique
delegation ID and support concurrent correlation.
Successful timing data stays in binary events. Certserver, request server,
middlebox, and gateway default to `-log_level error`; readiness, shutdown, and
errors remain visible in process logs. `-log_level debug` enables semantic
diagnostics such as quote presence/verification, delegation issuance, worker
selection state, and request/response handling. The middlebox only restores its
legacy text timestamp stream when `-minimal_logs=false` is also supplied.
The controller exposes the same policy through `client.log_level`, deployment
`gateway_log_level`, and deployment `worker_log_level`; all default to `error`.

## Request Server Instrumentation

Implemented in `cmd/appserver` using the same build-tagged tracer and flags as
the other Go components:

- `requestserver_request_start` is emitted after the complete request body is read;
- `requestserver_response_start` is emitted immediately before headers/body are written;
- `requestserver_response_done` records completion or a write error;
- all events use the forwarded `X-Trace-ID`.

### End-To-End Flow Correlation (Implemented Locally)

One client request ID cannot naturally be visible everywhere before the TLS
handshake: the gateway is an encrypted L4 proxy, and the middlebox contacts the
certserver during TLS before it has received HTTP headers. Do not force the
request ID into SNI or add a benchmark-only TLS extension.

The implementation uses linked identifiers:

1. The client keeps the existing request trace ID and records the TCP local and
   remote tuple when a connection is created.
2. The gateway records both accepted client-side and dialed worker-side socket
   tuples under its connection ID.
3. The middlebox records its TLS connection tuple. When HTTP headers arrive, it
   emits a binding event between that connection ID and `X-Trace-ID`.
4. The middlebox creates a delegation ID for each certserver call, emits a
   binding from TLS connection ID to delegation ID, and sends the delegation ID
   to certserver in the request body/header.
5. The application server records the forwarded `X-Trace-ID` directly.
6. The middlebox binds its dedicated upstream-session ID to the same connection
   key, allowing eager upstream TCP/TLS work to be attributed to the downstream
   handshake before HTTP headers exist.

The plotting analysis resolves these bindings into one canonical flow ID, so
the final processed table can present one request flow even though transport,
TLS delegation, and HTTP layers use different native identifiers.

The merge must preserve both the canonical request ID and each native ID. It
must never join solely on timestamps. For persistent connections, multiple
request IDs bind to one TLS connection ID; for fresh/resumption, one measured
request normally binds to one connection ID. Delegation IDs bind the certserver
work to the TLS connection that requested it.

The client-side `client_tls_resumed` event preserves the existing
`client_tls_done` error argument. The following dissection events are now
implemented:

- middlebox upstream request-sent and response-first-byte events;
- middlebox downstream response-first/last-byte events;
- gateway first-byte events in both splice directions.

Still add a clock-synchronization record/check for every participating host.

Without synchronized clocks, only durations measured within one process/host
and client end-to-end latency are safe. Cross-host one-way segments must remain
an explicit `network/unattributed` remainder.

## Missing Shutdown Cases

### Client Ctrl+C

Timed client runs flush correctly on normal exit. Ctrl+C can still bypass useful cleanup unless the client handles SIGINT/SIGTERM explicitly.

Add graceful signal handling to `cmd/client` if interactive aborted runs need complete trace files.

### Middlebox `exit_after_request`

The normal standalone middlebox now flushes on Ctrl+C through graceful server shutdown.

The `-exit_after_request=true` path still calls `os.Exit(0)`, which bypasses deferred trace flushing. Replace that with graceful shutdown if traced single-use workers are needed.

## Analysis Tools

The current converter emits one CSV per trace file:

```text
timestamp_ns,event_code,event_name,id,arg
```

Still useful to add:

- A merge tool that reads multiple `.bin` or `.csv` files and emits one timestamp-sorted CSV.
- A duration tool that computes common deltas, for example client TLS start to done, gateway queue enter to leave, worker selection time, container create to ready, and middlebox delegation fetch time.
- Optional per-event documentation for `arg`, since it is currently a generic value whose meaning depends on the event.

## Operational Notes

Worker trace files may stay empty while the worker process is still alive
because the writer is buffered. SGX worker teardown now sends `SIGTERM`, which
Gramine injects into the Go application, waits up to three seconds for a clean
exit, and only then falls back to forced removal. The controlled Docker+SGX
trace smoke verifies that worker traces are non-empty after teardown.

For Docker worker traces:

```bash
DOCKER_WORKER_TRACE_ENABLED=true
DOCKER_WORKER_TRACE_HOST_DIR=/path/on/docker/host
DOCKER_WORKER_TRACE_CONTAINER_DIR=/trace
```

The gateway's own `-trace /trace/gateway.bin` path is independent from worker trace settings.

Keep `-trace-drop-on-full=true` for latency experiments. Use `false` only when complete traces matter more than benchmark purity.
The controller now forwards both the trace-buffer capacity and drop policy to
Docker-created workers; the controlled dissection campaign therefore uses
blocking, complete traces consistently across every component.
