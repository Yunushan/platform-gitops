# Data Classification and Retention

This guide helps private deployment owners classify the data handled by the
platform and choose retention controls. It is not a legal policy. Keep actual
retention periods, data owners, system identifiers, customer names, and
regulatory mappings in the private deployment repository or governance system.

## Classification Levels

Use at least these classes before production:

| Class | Examples | Handling expectation |
|---|---|---|
| Public template data | Example manifests, public docs, placeholder values | Safe for this repository after validation |
| Internal deployment metadata | Private hostnames, internal service names, non-secret sizing, topology notes | Private repositories or internal systems only |
| Confidential operational data | Build logs, deployment history, alert history, vulnerability findings, audit trails | Access controlled, retained by policy, excluded from public issues |
| Restricted secrets and access material | Passwords, tokens, kubeconfigs, SSH keys, TLS keys, SOPS age private keys, robot credentials | Never plaintext in Git, rotate on exposure, store in approved secret systems |
| Regulated or customer data | Customer repositories, production application data, personal data, contractual records | Keep out of platform control-plane examples; handle under organization policy |

When a datum fits more than one class, use the most restrictive class.

## Component Data Map

| Component | Data commonly stored | Default class | Notes |
|---|---|---|---|
| Forgejo, Gitea, or GitLab | Git repositories, pull requests, users, SSH keys, webhooks, audit trail | Confidential or regulated | Classify each repository by its contents; do not mirror private repositories to this public template |
| Argo CD | Application specs, repository credentials, sync history, cluster destinations | Confidential | Keep repository credentials in Kubernetes Secrets or external secret systems |
| Woodpecker CI | Build logs, pipeline secrets, agent tokens, OAuth credentials | Confidential or restricted | Treat logs as sensitive when they can include build arguments, image names, or failure output |
| Harbor | Images, Helm artifacts, vulnerability reports, robot accounts | Confidential | Retention policy should match release and rollback requirements |
| CloudNativePG PostgreSQL | Application databases and WAL archives | Confidential or regulated | Classify by application; prove backups and point-in-time recovery |
| Longhorn or alternate storage | PVC contents, snapshots, volume backups | Matches workload data | Storage snapshots inherit the highest class of the source workload |
| Velero and object storage | Kubernetes object backups, PVC backups, backup metadata | Confidential or restricted | Backup credentials and restore authority are restricted |
| Loki | Application and platform logs | Confidential | Redact secrets before ingestion and keep retention short enough for the risk profile |
| Prometheus | Metrics, labels, target metadata, alert state | Internal or confidential | Labels can reveal topology, service names, tenant names, or incident detail |
| Grafana | Dashboards, datasource credentials, users, alert history | Confidential or restricted | Datasource credentials are restricted even when dashboards are internal metadata |
| cert-manager and trust-manager | Certificates, issuer references, trust bundles | Confidential | TLS private keys and CA material are restricted |
| step-ca | CA configuration, keys, provisioners, issued certificate records | Restricted | Backup, restore, and access controls must be reviewed before production |
| RKE2 and etcd | Kubernetes objects, Secrets, service accounts, control-plane state | Restricted | Etcd snapshots can contain every Kubernetes Secret in the cluster |
| CI runners and operator workstations | Workspace caches, kubeconfigs, SSH keys, registry sessions | Restricted | Prefer ephemeral runners and encrypted workstation storage |

## Retention Baseline

Define private retention values before production. The public template should
only describe the categories:

| Area | Retention decision |
|---|---|
| Git repositories and pull requests | Keep according to source retention, legal hold, and audit policy |
| CI logs and artifacts | Keep long enough to debug releases, then expire automatically |
| Registry artifacts | Keep release artifacts and rollback windows; prune unreferenced development images |
| Vulnerability scan results | Keep through the remediation and audit window |
| Metrics | Keep according to SLO, capacity, and incident review needs |
| Logs | Keep the shortest useful operational window unless legal or audit policy requires longer |
| Database backups and WAL | Match RPO/RTO and recovery-point requirements |
| Volume snapshots and backups | Match workload data class and restore requirements |
| Etcd snapshots | Keep enough generations for rollback and disaster recovery; protect as restricted data |
| Velero backups | Match Kubernetes object restore requirements and object-storage cost limits |
| Audit and access review evidence | Keep according to security governance policy |
| Incident records | Keep in the private incident system, not in public issues |
| Secrets and credentials | Rotate on schedule and immediately after exposure; do not rely on retention as protection |

## Handling Rules

- Keep public template data free of real internal domains, private IPs,
  credentials, customer names, and production evidence.
- Store private deployment metadata in private Git repositories or internal
  systems only.
- Encrypt restricted secrets with SOPS, External Secrets, Sealed Secrets,
  Vault/OpenBao, or another approved system.
- Treat backups, snapshots, and exported support bundles as sensitive as the
  highest-class source data they contain.
- Redact logs before sharing outside the organization.
- Limit production data in lower environments; use synthetic data for restore
  drills whenever possible.
- Review retention whenever profiles, storage backends, object storage,
  logging, monitoring, or registry settings change.
- Use `docs/CAPACITY_PLANNING.md` when retention, backup scope, registry
  pruning, log volume, metric retention, or storage sizing decisions are driven
  by growth pressure.
- Use `docs/COMPLIANCE_AUDIT.md` when data classification, retention, disposal,
  evidence collection, or audit exports must be mapped to private controls.

## Disposal and Erasure

For private deployments, define a disposal process that includes:

- Deleting or archiving data in the source system.
- Pruning registry artifacts, logs, metrics, snapshots, and backups according
  to retention policy.
- Rotating credentials after exposure or ownership change.
- Confirming Argo CD does not recreate deleted resources from Git.
- Recording the action in a private change, incident, or access-review record.

Do not promise customer or user erasure until backups, replicas, caches,
archives, and legal/audit holds have been reviewed.

## Evidence

Keep private evidence for:

- Current data owner for each platform component.
- Current retention period for each data class and component.
- Latest access review for restricted data.
- Latest access-control review from `docs/ACCESS_CONTROL.md`.
- Latest backup and restore proof from `docs/BACKUP_RESTORE.md`.
- Latest alert/log review from `docs/ALERTING.md`.
- Latest threat-model review from `docs/THREAT_MODEL.md`.
- Latest incident response review from `docs/INCIDENT_RESPONSE.md`.
- Latest capacity planning review from `docs/CAPACITY_PLANNING.md`.
- Latest compliance and audit evidence review from `docs/COMPLIANCE_AUDIT.md`.
- Latest secret rotation or exposure handling record from `SECURITY.md`.

Do not commit private retention schedules, data inventories, or customer
classification records to this public template.
