# TLMSP `/function/init` Smoke Path

Working path:

```text
tlmsp-curl -> tlmsp-mb :10001 -> TLMSP Apache :4444 -> plain HTTP backend :7000
```

The Go `listener` stays on `:8080`; `ETSI/NewMiddlebox/client` posts request and response contexts to it. Do not put the backend on `:8080`.

Run the local setup in separate terminals:

```bash
/home/bonsai/dcmb/PerformanceMeasuring/tlmsp_init_smoke.sh backend
/home/bonsai/dcmb/PerformanceMeasuring/tlmsp_init_smoke.sh httpd
/home/bonsai/dcmb/PerformanceMeasuring/tlmsp_init_smoke.sh listener
/home/bonsai/dcmb/PerformanceMeasuring/tlmsp_init_smoke.sh middlebox
```

Then smoke test:

```bash
/home/bonsai/dcmb/PerformanceMeasuring/tlmsp_init_smoke.sh check-backend
/home/bonsai/dcmb/PerformanceMeasuring/tlmsp_init_smoke.sh curl
```

Expected result is HTTP `200 OK` with:

```json
{"status": "ok", "message": "function initialized"}
```

Useful notes:

- `certs_server.py` is not used in this plain HTTP backend path.
- The Apache vhost is installed at `.tlmsp/etc/apache24/httpd_tlmsp.conf`.
- The source copy is `ETSI/httpd_tlmsp_local.conf`.
- If the installed config is overwritten by a TLMSP rebuild, run:

```bash
/home/bonsai/dcmb/PerformanceMeasuring/tlmsp_init_smoke.sh install-config
```

- `ssl error 5` with empty OpenSSL queue after a successful response appears to be normal connection teardown noise in `tlmsp-mb`.
