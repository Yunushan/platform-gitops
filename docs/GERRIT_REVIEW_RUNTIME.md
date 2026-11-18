# Disposable real Gerrit review protocol acceptance

`ReporterTests.test_real_gerrit_discovery_checkout_and_review_protocol` exercises
the production [Gerrit submitter](TEKTON_GERRIT_BRIDGE.md), Git checkout and
[result reporter](TEKTON_GERRIT_RESULTS.md) against a disposable Gerrit 3.13.10
distribution. It uses no company endpoint, borrowed account, existing repository,
SMTP relay or production kubeconfig.

The scope is native Gerrit authentication, API/review policy and Git protocol.
Kubernetes clients, PipelineRun/TaskRun/Pod records and controller status remain
explicitly modeled. Real local checkout/application tests do not establish
Tekton worker execution. Fleet deployment, HA plugins, shared Ceph storage, SSO,
physical-node failure, CI/CD, upgrades and restore remain separate requirements.

## Pinned public prerequisites

Prepare approved public artifacts in ignored private storage:

| Artifact | SHA-256 |
|---|---|
| Gerrit `gerrit-3.13.10.war` | `3e7d3a354e07c5aa90d25a7a3863b3df615515f168afdaf5cff0357b066a9297` |
| Temurin `OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz` | `a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd` |

The WAR is linked from the official [3.13 release page](https://www.gerritcodereview.com/3.13.html).
Its bytes were compared with Maven Central's versioned checksum. This is not
maintainer-signature verification or approval for a production upgrade. The
fixture downloads nothing during execution and refuses changed, symlinked or
oversized inputs. The Java archive is also used by the identity acceptance fixture.

Use a disposable root Linux amd64 runner with Python/PyYAML, Git, OpenSSL,
iproute2 and an executable Linux temporary filesystem. A new empty network
namespace is mandatory. The fixture enables loopback and binds fresh local
ports. Gerrit init/daemon execute as `nobody` with parent-death protection;
the supervising service should impose runtime/memory/process bounds. Original
processes and temporary scope are removed before a successful receipt is written.

## Authentication and protocol exercised

The fresh site skips downloads and disables mail/SSH. A private loopback TLS
proxy supplies HTTP-mode login identities only for three synthetic users.
Browser bootstrap uses normal Gerrit sessions/XSRF to generate short-lived
**native Gerrit API tokens**. Production helpers use those tokens at `/a/`.
Proxy login credentials are not native tokens: a deliberately invalid token is
allowed through the proxy solely to verify Gerrit's own rejection. See the
[account-token API](https://gerrit-review.googlesource.com/Documentation/rest-api-accounts.html#create-auth-token).

The fixture provisions one project, a non-copying Verified label and a dedicated
voting group. The bot receives the intended voting range and configuration-read
access for label inspection. Reader voting and bot policy mutation are checked
as denied operations. Fixture administration stays outside the production helper.

Actual edit/publish creates a patch set containing a small application test.
The production source helper discovers/verifies its change/ref/commit. Production
checkout fetches that real ref over authenticated HTTPS and runs the local test.
Using explicit modeled CI records, the result helper sends a synthetic result
to the real Gerrit API and verifies its actual tagged message and detailed bot
vote. Duplicate delivery must not republish. A new real patch set invalidates
the old result: stale submission/reporting are refused and the earlier approval
must not be copied to the untested revision. The fixture also checks its local
site configuration for absent copy-enforcement overrides. Such site settings
are not exposed by the inspected label/server-info REST APIs; external
deployment policy remains a separate acceptance requirement.

No credentials, cookies, raw responses or review identities enter the aggregate
receipt. Generated JSON is checked for ephemeral tokens. Raw Java output stays
in the disposable private scope and is removed during cleanup.
After safe extraction of the checksum-verified Java distribution, its actual
runtime directories receive traversal permissions inside the enclosing `0700`
private scope. This supports a `0077` service umask without exposing credentials
or changing the host umask; root-owned `0700` extraction directories otherwise
prevent the unprivileged Java process from starting.

The separate [K3s worker case](K3S_VM_WORKERS.md) adds a live Tekton build and
scoped result-reporting path on actual VM workers. It has its own opt-in
selection and acceptance receipt; this protocol-only case's modeled records
do not establish that additional worker execution.

## Execute explicitly

```bash
sudo env \
  GERRIT_REVIEW_TEST_RUNTIME=1 \
  GERRIT_REVIEW_TEST_ARTIFACTS=/absolute/private/cache/gerrit/3.13.10 \
  GERRIT_REVIEW_TEST_JAVA_ARCHIVE=/absolute/private/cache/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz \
  /usr/bin/unshare --net -- \
  python3 -B scripts/test_tekton_gerrit_reporter.py \
  ReporterTests.test_real_gerrit_discovery_checkout_and_review_protocol
```

`GERRIT_REVIEW_TEST_REPORT` may select a new ignored `private/` output within
this checkout. Receipts are create-only mode `0600`, include first-party source
hashes and are written after process/scope cleanup. Freeze Python sources while
an acceptance run is active.

Narrow protocol flags concern only the tested Gerrit flow. Broader
`gerrit_runtime_verified`, `tekton_execution_verified`, `review_vote_verified`,
CD, HA and full-stack flags remain false because complete application,
controller/worker and deployment acceptance are not exercised here.
