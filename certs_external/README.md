# Local Certificates

After cloning the repository, generate the static certificate material used by
`PerformanceMeasuring/certs_server.py`:

```bash
cd certs_external
./generate_server_certs.sh
```

This creates `ca.crt`, `ca.key`, `server/cert.pem`, `server/key.pem`, and the
CSR/config files used to build them. The server leaf certificate includes the
Delegated Credentials extension, so it can sign runtime delegated credentials.

The generated `dc.cred` and `dckey.pem` files are intentionally not created by
this script. They are produced by the certificate server whenever the `/certs`
endpoint is requested.
