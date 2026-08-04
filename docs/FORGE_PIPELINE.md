# Pipeline Compatibility And Conversion

`scripts/forge_pipeline.py` is the pipeline-semantic gate for a GitLab or
GitHub migration. It converts a supported subset of a source pipeline into a
deterministic Woodpecker file and writes a redacted compatibility report.

It is intentionally not a best-effort YAML rewrite. The command exits nonzero
when a source feature would change execution, security, scheduling, artifact,
runner, or deployment semantics. The migration must then provide an explicit
manual mapping or a provider-specific implementation before cutover.

## Convert A Pipeline

Run the converter outside the repository's tracked tree or write the result to
the destination repository's working tree for review:

```bash
make forge-pipeline-convert \
  PROVIDER=gitlab \
  SOURCE=/work/app/.gitlab-ci.yml \
  OUTPUT=/work/app/.woodpecker.yml \
  REPORT=private/migrations/proof/app-pipeline.json \
  GATE_MARKER=FORGE_CUTOVER_DEPLOYMENT_ENABLED \
  DEPLOYMENT_JOB=deploy \
  RUNNER_LABEL='ubuntu-latest=platform:linux/amd64' \
  DEFAULT_IMAGE=alpine:3.20
```

For GitHub Actions, use `PROVIDER=github` and point `SOURCE` at one workflow
file. Repeat the command for every workflow that is intentionally mapped to a
Woodpecker pipeline. Review the report and the generated YAML in code review;
the converter never commits either file to a remote provider.

Pass a space-separated `SECRET` value for provider variables whose names are
not obviously secret, and a space-separated `DEPLOYMENT_JOB` value for every
deployment job. Obvious secret references are detected automatically, but
explicit secret mapping is preferred for production cutovers. Repeat
`RUNNER_LABEL=SOURCE=KEY:VALUE[,KEY:VALUE]` for each source runner label and
`SCHEDULE_MAPPING='SOURCE_CRON=WOODPECKER_CRON_NAME'` for a schedule used by
the source pipeline. The Make target passes this as one quoted argument so the
cron's spaces remain intact; separate multiple mappings with `;`. These
options contain names and labels only, never secret values.

## Supported Mapping

The converter preserves or maps these common constructs:

- GitLab jobs, stages, scripts, images, services, variables, `before_script`,
  `after_script`, `needs`, basic branch/event rules, tags, and manual/always
  execution conditions.
- GitHub jobs, individually scoped `run` steps, checkout,
  setup-node/setup-python/setup-go, job dependencies, job and step
  environments, positive push/pull-request branch and path filters, manual
  triggers, and runner-label mappings.
- Secret references, including supported GitHub `${{ secrets.NAME }}` and
  `${{ vars.NAME }}` expressions, become Woodpecker `from_secret` references.
  Source secret values are never written to generated YAML or the report.
- Deployment-like jobs receive the configured fail-closed deployment gate.
  Every deployment job must be listed with `DEPLOYMENT_JOB` or the conversion
  fails rather than guessing.

## Deliberate Hard Failures

The converter blocks unsupported or lossy constructs, including GitHub
reusable/third-party actions, matrices, artifact/cache semantics, permissions,
concurrency, protected environments, GitLab `extends`, triggers, artifacts,
cache, parallel jobs, complex rules, and unresolved external includes. Runner
names are not machines: the target Woodpecker agents and labels must be
created separately and verified by the cutover or transition proof.

Schedules are also separate resources. A GitHub schedule or GitLab pipeline
schedule must be explicitly mapped to a disabled Woodpecker cron entry during
shadow preparation, then enabled only during the approved cutover. The
converter accepts a GitHub schedule only when the migration plan supplies its
source cron to Woodpecker cron-name mapping. Event-specific GitHub filters
that cannot share one workflow-level Woodpecker filter are blocked rather than
flattened.

This boundary is what makes the migration claim defensible: every source CI
surface is either converted and reviewed, explicitly mapped, accepted for
manual handling, or blocks activation. A green repository-transfer proof alone
is not proof that arbitrary provider-specific CI behavior was preserved.

## Cutover Ordering

Use the converter before `forge-cutover-prepare` or
`forge-transition-prepare`. Commit the reviewed Woodpecker file to the
destination repository, then include it in the plan's pipeline mapping. Keep
deployment disabled during shadow verification. Only the explicit `activate`
or `enter` operation may disable source CI and grant Woodpecker/Argo CD
deployment authority. The existing rollback/failback proof remains the source
of truth for restoring the old CI authority.
