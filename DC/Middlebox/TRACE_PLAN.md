# Trace Remaining Work

Current state:

- `internal/trace` exists with build-tagged no-op/active implementations.
- `cmd/client`, `cmd/gateway`, and `cmd/middlebox` accept `-trace`, `-trace-buffer-events`, and `-trace-drop-on-full`.
- Client, gateway, Docker container lifecycle, and middlebox worker paths have trace probes.
- Docker workers can write per-worker trace files when `DOCKER_WORKER_TRACE_ENABLED=true`.
- `cmd/tracecsv` converts binary trace files to CSV.
- `compile.sh` builds trace-enabled binaries with `-tags trace`.

## Missing Instrumentation

### Certserver

Add trace support to `cmd/certserver`:

- Add the common trace flags.
- Call `trace.Start(...)` and `trace.Stop()`.
- Mark `/certs` request received.
- Mark delegated credential generation start/done.
- Mark response written.
- Mark errors.

Potential correlation:

- The middlebox cannot receive the client HTTP `X-Trace-ID` before the TLS handshake completes, so delegation/certserver events are currently best correlated by time and SNI.
- If needed later, add a middlebox-generated delegation ID and send it to certserver in the `/certs` JSON body or an HTTP header.

### Request Server

Instrument the final application/request server:

- request received
- application processing start/done
- response written
- error

If the request server remains Python, either add a small compatible binary writer there or write CSV directly with the same event names.

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

Worker trace files may stay empty while the worker process is still alive because the writer is buffered. They should become non-empty after the worker exits gracefully or the gateway removes it gracefully.

For Docker worker traces:

```bash
DOCKER_WORKER_TRACE_ENABLED=true
DOCKER_WORKER_TRACE_HOST_DIR=/path/on/docker/host
DOCKER_WORKER_TRACE_CONTAINER_DIR=/trace
```

The gateway's own `-trace /trace/gateway.bin` path is independent from worker trace settings.

Keep `-trace-drop-on-full=true` for latency experiments. Use `false` only when complete traces matter more than benchmark purity.
