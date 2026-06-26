# trust-manager

Distributes trust bundles to namespaces. The default bundle publishes public
CA roots from the trust-manager package into ConfigMaps.

For an internal root CA, copy `internal-roots.example.yaml` into a private
overlay, replace the placeholder with the PEM-encoded root certificate, and
keep the private key outside Git.
