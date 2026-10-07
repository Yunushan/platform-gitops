# Private system mail

Configure a shared system sender and administrative notification recipient without
publishing the relay, company mailbox, domains, or credentials. This is opt-in;
the public defaults do not deploy these overlays. Admin **login identities**,
migrated users' emails/passwords, storage, OAuth, SSO, and image versions are not
changed by mail preparation.

## Prepare

Copy [the dedicated env example](../config/system-mail.env.example) to
`private/system-mail.env` and populate it privately. The helper supports the
existing unauthenticated IPv4 relay mode on TCP/25 only. Confirm the mail team
permits the actual cluster/build-pod source addresses and sender. If credentials,
mandatory STARTTLS/SMTPS, a relay DNS name, or a private CA are required, review
that configuration separately; do not turn off certificate verification.

Run from the template checkout, using the **current internal GitOps values** as
inputs so existing notification settings are preserved:

```bash
python3 scripts/configure_system_mail.py \
  --env-file private/system-mail.env \
  --forgejo-values /path/to/internal-checkout/apps/forgejo/values.yaml \
  --argocd-values /path/to/internal-checkout/apps/argocd-ha/values.yaml \
  --output-dir private/system-mail
```

This is read-only planning. Repeat with `--write` to produce ignored local files.
No cluster mutation, SMTP connection, email, or migration occurs. With omitted
values paths it reads public templates: those artifacts are only preparation,
not confirmation that current company notification settings were preserved.
Conflicting SMTP authentication/protocols or existing Argo email configuration
stop preparation. Existing outputs must be identical; use a new ignored output
directory after changing inputs rather than overwriting an operator's files.

## Deploy through the internal source only

1. Verify the internal GitOps remote and current branch/revision. Never copy
   populated mail overlays into this public template checkout's tracked paths.
2. In the **internal** Forgejo and Argo CD app directories, add the respective
   generated mail values overlay as the last Helm values file (Kustomize
   `helmCharts[].additionalValuesFiles`). Keep the original `valuesFile`, chart,
   release name, databases and PVCs unchanged. Existing Argo subscriptions are
   included in the prepared overlay: regenerate it against current values before
   promotion if those settings changed. Do not run full first-deploy rendering
   or a `helm upgrade` outside Argo CD merely to change mail.

   Example addition to the existing **internal** chart entry (retain its other fields):

   ```yaml
   additionalValuesFiles:
     - system-mail-values.yaml
   ```

   Copy the generated overlay to that app-local filename. Populated overlays
   belong only to the internal repository, never to this public one.
3. Add the two documents in `smtp-egress.yaml` to the appropriate internal app
   resources. Argo permits only the notification controller to the relay's /32
   on TCP/25. Woodpecker permits its namespace's build pods to that same relay
   and port. If the build backend runs elsewhere, adapt the **private** policy's
   namespace before promotion. This does not remove or relax other policies;
   existing Forgejo SMTP egress is already provided by its component. Cilium
   deny rules and external firewall restrictions may still need inspection.
4. Review the internal rendered diff: only mail configuration and the scoped
   egress policies should change. Commit to the **internal** source, then let
   Argo CD reconcile. It is not enough to change local files or live objects
   that self-heal would revert.

Forgejo enables native mail and user mail notifications with the shared sender;
it does not redirect users' normal notification recipients to the admin mailbox.
Argo CD adds minimal failed-sync/degraded-health notifications to the shared
mailbox. It preserves unrelated notifiers, triggers, templates, and subscriptions.
OutOfSync/Progressing are not automatically treated as failures or fixed by mail.

## Woodpecker pipelines

Woodpecker has no global native SMTP/admin-email switch. Preserve `WOODPECKER_ADMIN`
as authorized Forgejo usernames. Add `woodpecker-mail-step.yaml`'s step to the
**intended** workflows, after build/test steps; do not replace a repository's
existing workflow with this fragment or create an independent success-only workflow.
For DAG workflows set dependencies to the intended build/test steps as well.

In Woodpecker's repository or organization secret store, add these private values
from `woodpecker-mail-secrets.env` (this file is **not** a shell deployment env):

- `system_mail_dsn`: unauthenticated SMTP DSN with peer verification enabled;
  automatic STARTTLS remains enabled by default.
- `system_mail_from`: the complete JSON sender object, not just the email string.
- `system_mail_to`: the shared mailbox.

Restrict each secret to the pinned email plugin image and approved push/tag/manual
events. Do not enable pull-request access or unrestricted global exposure. The
fragment uses `from_secret`, emails only the explicit recipient, attaches no
logs, and runs on failure. It does not install a server mailer or silently enable
notifications for every migrated repository. Review the external plugin and its
image against your registry/admission policy before enabling it; the prepared
3.3.0 multi-platform digest was checked against the publisher on 2026-10-01.
The generated image reference is digest-only (without a version tag): Woodpecker
3.16's secret validator rejects dotted tags such as `3.3.0`. Use that exact same
digest-only reference in both the workflow and the secret's plugin filter. Do
not work around the validator by allowing an untagged image or every plugin.

### Explicit plain SMTP for an approved private relay

If the mail team and operator explicitly approve **unencrypted** delivery to a
private relay, prepare a new ignored output directory with
`--woodpecker-plain-smtp`. The Woodpecker DSN then includes
`auto_tls=false&verify_peer=1`: automatic STARTTLS is disabled, without disabling
certificate checks for a TLS connection. There are no SMTP credentials in this
mode. This flag changes only the prepared Woodpecker DSN, not Forgejo or Argo CD.
See [Symfony's automatic TLS option](https://symfony.com/doc/7.2/mailer.html#disabling-automatic-tls).

The helper will not overwrite existing prepared outputs when changing modes.
Save the resulting DSN in the intended private Woodpecker secret store and keep
the plugin-image/event filters. Changing a local file alone does not update a
saved secret or activate workflow notifications. Global settings require an
explicit instance-wide scope decision. With no activated repositories, configure
the mail settings first, then select intended workflows separately; do not
activate every migrated repository to test SMTP.

## Verify and roll back

Verify controller readiness and SMTP egress from the **actual sender pods**, not
only from a node. Check effective Forgejo mailer settings (including any secret
environment overrides), the Argo notifications ConfigMap/subscriptions, and the
Woodpecker secret filters and workflow order without printing secrets. Then send
one authorized Forgejo test email, one Argo notification and one test CI failure
to the shared mailbox; confirm arrival and mail relay logs. TCP connectivity or
successful rendering alone is not proof of delivery. Existing certificate checks
remain enabled; fix trust/relay configuration rather than bypassing verification.

Revert only the internal mail-overlay references/policies and workflow mail step
to roll back. Do not restore databases, delete PVCs, or reset accounts for this.

References: [Kustomize Helm values](https://github.com/kubernetes-sigs/kustomize/blob/master/api/types/helmchartargs.go),
[Forgejo mailer settings](https://forgejo.org/docs/latest/admin/config-cheat-sheet/#mailer-mailer),
[Argo CD email](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/notifications/services/email/),
[Woodpecker plugin](https://woodpecker-ci.org/plugins/woodpecker-email),
[Woodpecker secret restrictions](https://woodpecker-ci.org/docs/usage/secrets).
