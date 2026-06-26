# trust-manager

Premium profile trust distribution. This runs two trust-manager replicas with
a PodDisruptionBudget and publishes public CA roots through a Bundle.

For company/internal roots, add the root certificate in a private overlay based
on `internal-roots.example.yaml`. Never commit CA private keys.
