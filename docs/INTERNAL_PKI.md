# Internal PKI

The premium profile uses cert-manager and trust-manager for private service
identity. This is separate from the wildcard certificate used at the Traefik
ingress boundary.

## Trust Model

- `cert-manager/platform-internal-root-ca` is a five-year ECDSA root whose key
  remains in the ignored private cluster state as a Kubernetes Secret.
- `ClusterIssuer/platform-internal-ca` signs short-lived internal service
  certificates.
- `Bundle/platform-internal-roots` distributes the public root together with
  operating-system roots. It never distributes `tls.key` or `ca.key`.
- OpenBao receives a 90-day server certificate with a 15-day renewal window.
- cert-manager issues the `platform-postgres-server` certificate from the
  platform internal CA. CloudNativePG consumes the resulting Secret for its
  server CA and server leaf, and reloads it when cert-manager renews it.
- cert-manager also issues `platform-valkey-server`. Valkey disables its
  plaintext listener, encrypts replication and Sentinel traffic, and reloads
  renewed leaf certificates every five minutes. HAProxy and the metrics
  exporter receive only the public trust bundle and verify the Valkey service
  or pod identity.
- Forgejo, Woodpecker, Harbor, Keycloak, and Grafana consume the
  `platform-internal-roots` bundle and require hostname verification with
  `verify-full`.
- Forgejo and Harbor use `rediss://` for the shared Valkey service. They mount
  only `platform-internal-roots`; the Valkey private key remains confined to
  the Valkey pods.

The bootstrap root uses `rotationPolicy: Never`. Root replacement is a planned
two-root ceremony: add the next public root to the bundle, issue and roll leaf
certificates from the next issuer, verify all clients, and only then remove the
old root. Do not rotate the root by deleting its Secret during routine renewal.

## OpenBao Rotation

OpenBao mounts `openbao-server-tls` and serves TLS on ports 8200 and 8201. A
non-root sidecar watches the certificate file. When cert-manager rotates the
leaf, the sidecar sends `SIGHUP` to OpenBao through the shared process namespace;
OpenBao then reloads the certificate and key from their original paths.

The injector receives `AGENT_INJECT_VAULT_CACERT_BYTES` from the
`platform-internal-roots` ConfigMap, so injected agents validate OpenBao rather
than skipping certificate verification. Prometheus uses the same bundle and a
fixed service DNS name when scraping OpenBao.

## Valkey Rotation

Valkey and Sentinel reload renewed leaf material through
`tls-auto-reload-interval 300`. HAProxy verifies the per-pod DNS identity using
the stable internal root, so routine leaf renewal does not require distributing
private key material or restarting clients. A root replacement still follows
the two-root ceremony described above.

## Verification

Run this against the private cluster:

```bash
make platform-internal-tls-verify
```

The proof fails unless the root, issuer, leaf certificate, and trust bundle are
Ready; the distributed bundle contains no private key; OpenBao verifies its own
service certificate; and PostgreSQL STARTTLS verifies the
`platform-postgres-rw.platform-databases.svc.cluster.local` identity. Sealed or
uninitialized OpenBao may return its documented status code, but a TLS trust
error never passes. The proof also queries `pg_stat_ssl` and requires active,
TLS-protected sessions for Forgejo, Woodpecker, Harbor, Keycloak, and Grafana.
It additionally verifies the Valkey leaf and hostname, authenticated TLS PINGs
through Valkey and Sentinel, HAProxy certificate verification, `rediss://`
client Secrets, and rejection of a plaintext Valkey command.

The live proof is part of `make platform-production-check`. Keep its output in
the private commit-bound production evidence package.
