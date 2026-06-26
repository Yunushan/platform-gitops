# cert-manager

Default platform certificate lifecycle component. It installs cert-manager CRDs
and controllers; trust distribution is handled by the separate trust-manager
app.

Add private `Issuer` or `ClusterIssuer` resources through private overlays or
encrypted secret workflows.
