# Security Policy

This repository is a public-safe platform template. It contains automation,
manifests, examples, and documentation for private GitOps deployments. It must not contain live organization secrets, customer data, private hostnames, private IP addresses, kubeconfigs, or production access details.

## Supported Scope

Security reports are in scope when they affect this template or a deployment
created from it:

- Bootstrap scripts, Ansible playbooks, Makefile targets, and validation tools.
- GitOps manifests, Helm values, profiles, and rendered private-value helpers.
- CI definitions for GitHub, GitLab, Gitea, Forgejo, and Woodpecker.
- Secret/privacy scanners and policy examples.
- Documentation that could cause unsafe production operation.
- Supply-chain helper configuration such as Renovate and Cosign/Kyverno
  examples.

Reports about third-party upstream products such as RKE2, Cilium, Argo CD,
Forgejo, Woodpecker, Harbor, Longhorn, CloudNativePG, Traefik, cert-manager,
Loki, Prometheus, Grafana, Velero, or container images should also be reported
to the relevant upstream project. If this template pins or documents an unsafe
version or setting, report it here too.

## Supported Versions

This template follows a latest-main security support model until tagged release
branches are published.

| Version | Security support |
|---|---|
| `main` | Supported |
| Released tags | Best effort unless a release branch states otherwise |
| Private deployment forks | Supported by the owning organization |

Private deployment repositories should document their own supported versions,
owners, and patch windows in their private operations records.

## Reporting a Vulnerability

Use a private security report through your Git hosting platform when available.
For private organizational deployments, use your internal security process and
include the platform maintainers only through approved private channels.

Do not paste any of the following into public issues, public pull requests,
chat, screenshots, or logs:

- Real secrets, tokens, passwords, or private keys.
- Kubeconfigs or SSH keys.
- Internal IP addresses, private DNS names, or customer domains.
- Customer data, user data, or production logs.
- Working exploit details against a live private deployment.

When reporting, include safe details:

- Affected file, target, profile, or component.
- Safe reproduction steps using placeholders or a local lab.
- Expected and actual behavior.
- Impact and whether exploitation requires a private deployment value.
- Suggested fix, mitigation, or upstream advisory when known.

## Response Expectations

For this public template, maintainers should triage reports using this model:

| Severity | Example | Target first response |
|---|---|---|
| critical | Secret exposure path, default unsafe remote access, production data loss path | 2 business days |
| high | Auth bypass in automation, unsafe bootstrap default, exploitable supply-chain drift | 5 business days |
| medium | Misleading security documentation, missing validation guard, risky optional config | 10 business days |
| low | Hardening improvement, unclear wording, defense-in-depth suggestion | Best effort |

Private deployment owners should use their own incident response targets when a
vulnerability affects a live cluster.

## Secret Exposure Handling

If a secret, kubeconfig, private key, private hostname, internal IP, or customer
identifier is committed:

1. Stop pushing more changes.
2. Rotate the exposed credential or key immediately.
3. Revoke tokens, sessions, and robot accounts that may have used it.
4. Remove the value from the working tree.
5. Run `make no-secrets`.
6. Run `python scripts/run_validation.py`.
7. Rewrite Git history only in private repositories and only after coordinating
   with all users of the repository.
8. Document the incident in the private operations record.

Removing a secret from the latest commit is not enough. Treat it as exposed.

## Dependency and Supply-Chain Security

The repository includes:

- `renovate.json` for dependency tracking, grouped Helm updates, and Docker
  digest pinning.
- CI workflows pinned to full commit SHAs for third-party Actions-style steps.
- `policies/kyverno/verify-signed-images.example.yaml` as an opt-in
  Cosign/Kyverno verification example.
- Validation for pinned Helm charts and curated image tags/digests.
- `scripts/validate_no_secrets.py` and `make no-secrets` for public-safety
  scanning.

Private deployments should decide when updates are promoted, how images are
signed, where SBOMs and attestations are stored, and which namespaces enforce
signature verification.

## Secure Configuration Baseline

Before production use:

- Run `python scripts/run_validation.py`.
- Run `make no-secrets`.
- Run `PLATFORM_PROFILE=premium-3node make platform-production-check` against
  the target cluster.
- Complete the go/no-go checklist in `docs/PRODUCTION_READINESS.md`.
- Complete the restore drill in `docs/BACKUP_RESTORE.md`.
- Complete the continuity exercise in `docs/BUSINESS_CONTINUITY.md`.
- Review service ownership and criticality in `docs/SERVICE_CATALOG.md`.
- Review significant platform decisions in `docs/ARCHITECTURE_DECISIONS.md`.
- Follow day-2 controls in `docs/OPERATIONS.md`.
- Use `docs/INCIDENT_RESPONSE.md` for incident roles, communications,
  recovery validation, and post-incident review.
- Use `docs/ACCESS_CONTROL.md` for identity, RBAC, robot accounts,
  break-glass access, and access-review evidence.
- Use `docs/CAPACITY_PLANNING.md` for capacity domains, saturation signals,
  load tests, scaling decisions, and private evidence.
- Use `docs/COMPLIANCE_AUDIT.md` for control domains, audit evidence,
  exception handling, and private review cadence.
- Use `docs/RELEASE_PROMOTION.md` for environment promotion gates, rollback,
  hotfix, freeze, and release evidence.
- Define alert routing and SLO evidence with `docs/ALERTING.md`.
- Review assets, trust boundaries, and high-risk changes in
  `docs/THREAT_MODEL.md`.
- Classify component data and retention decisions with
  `docs/DATA_CLASSIFICATION.md`.
- Keep SOPS age private keys and external secret credentials outside Git.
- Keep Argo CD pointed at a private deployment repository, not a temporary seed Git URL.

## Disclosure and Safe Harbor

Good-faith research against local labs, disposable clusters, or your own private
deployment is welcome. Do not test against systems you do not own or operate.
Do not exfiltrate data, persist access, disrupt service, or access customer
information.

Public disclosure should wait until a fix or mitigation is available, unless
the vulnerability is already public through an upstream advisory. Coordinate
with private deployment owners before disclosing details that could identify or
harm their environments.
