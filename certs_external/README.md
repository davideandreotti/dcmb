# Local Certificates

After cloning the repository, generate the static certificate material used by
the Go certificate server in `DC/Middlebox/cmd/certserver`:

```bash
cd certs_external
./generate_server_certs.sh
```

This creates `ca.crt`, `ca.key`, `server/cert.pem`, `server/key.pem`, and the
CSR/config files used to build them. The server leaf certificate includes the
Delegated Credentials extension, so it can sign runtime delegated credentials.

The generated `dc.cred` and `dckey.pem` files are intentionally not created by
this script. The Go certificate server produces delegated credentials whenever
its `/certs` endpoint is requested.
