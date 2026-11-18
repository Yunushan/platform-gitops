# gitea

Enable through `profiles/gitea-woodpecker-argocd.yaml`. Kustomize uses the
vendored Gitea chart 12.7.0 with a digest-pinned Gitea 28.3.0 rootless image.
The profile uses one Gitea replica with SQLite on persistent storage.

Before the first sync, use your private secret management workflow to create
`gitea-bootstrap-admin` in the `gitea` namespace with `username` and
`password` keys. The bootstrap administrator must change the initial password
at first sign-in. Provide the `gitea-tls` Secret in that namespace for the
rendered ingress hostname.
