#!/usr/bin/env python3
"""Prepare ignored, mail-only Helm overlays and a Woodpecker notification step.

Never deploys, sends mail, rewrites users, or re-renders stateful application values.
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
from pathlib import Path
import re
import sys

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from strict_yaml import loads_strict_yaml_all

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "gitops/clusters/rke2-main/premium-3node/apps"
# Plugin 3.3.0's multi-platform digest, checked against the publisher's registry.
# Digest-only syntax also works in Woodpecker 3.16's secret-image validator,
# which rejects dotted tags. Workflow and secret filter must use the same ref.
EMAIL_IMAGE = (
    "deblan/woodpecker-email@sha256:"
    "3b80a244b42e9e6f7e66d93873e2da1f69e6b19de6f71bc9a052f76a632ad9b2"
)
MANAGED_TRIGGERS = ["platform-mail-sync-failed", "platform-mail-health-degraded"]


class MailError(ValueError):
    """Only fixed, non-private diagnostic messages may escape to the console."""


def read_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise MailError("Expected a regular input file; private path suppressed")
    return read_bounded_bytes(path, max_bytes=4 * 1024 * 1024).decode("utf-8")


def document(text: str) -> dict:
    docs = loads_strict_yaml_all(text)
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise MailError("Expected one unambiguous YAML mapping")
    return docs[0]


def mapping(value: object) -> dict:
    if not isinstance(value, dict):
        raise MailError("Existing mail configuration has an unsupported shape")
    return value


def private_output(path: Path, root: Path = ROOT) -> Path:
    """Require an ignored AND untracked path underneath this checkout's private/.

    Do not follow symlinks/junctions to an outside destination. Ignored files can
    still be tracked via git add -f, so checking ignore rules alone is insufficient.
    """
    path = path.absolute()
    private = root / "private"
    if (private.resolve() != private.absolute() or not path.is_relative_to(private)
            or not path.resolve().is_relative_to(private.resolve())):
        raise MailError("Mail outputs must remain underneath the ignored private directory")
    for parent in (private, *path.relative_to(private).parents):
        candidate = parent if parent.is_absolute() else private / parent
        if candidate.is_symlink():
            raise MailError("Symlinked private output paths are refused")
    if path.is_symlink():
        raise MailError("Symlinked private output paths are refused")
    for arguments in (
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(path)],
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", str(path)],
    ):
        result = run_bounded(arguments, text=True, timeout=10, check=False, output_max_bytes=65536)
        if arguments[3] == "check-ignore" and result.returncode != 0:
            raise MailError("A mail output is not ignored by Git")
        if arguments[3] == "ls-files" and result.returncode != 1:
            raise MailError("A mail output is tracked or its Git boundary could not be verified")
    return path


def load_settings(path: Path) -> dict:
    """Read a dedicated KEY=value file without sourcing/evaluating shell code."""
    settings = {}
    allowed = {"PLATFORM_SMTP_HOST", "PLATFORM_SMTP_PORT", "PLATFORM_SYSTEM_EMAIL",
               "FORGEJO_MAILER_ENABLED", "FORGEJO_MAILER_PROTOCOL", "FORGEJO_MAILER_HOST",
               "FORGEJO_MAILER_PORT", "FORGEJO_MAILER_FROM"}
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in allowed or key in settings:
            raise MailError("Use a dedicated system-mail env file with unique supported keys")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if any(char in value for char in "\r\n\x00$`"):
            raise MailError("Shell expressions and control characters are refused in mail settings")
        settings[key] = value
    host, email = settings.get("PLATFORM_SMTP_HOST", ""), settings.get("PLATFORM_SYSTEM_EMAIL", "")
    try:
        address = ipaddress.ip_address(host)
        port = int(settings.get("PLATFORM_SMTP_PORT", "25"))
    except ValueError as exc:
        raise MailError("Supply a literal SMTP relay address and port 25") from exc
    if address.version != 4 or port != 25 or address.is_unspecified or address.is_multicast or address.is_loopback:
        raise MailError("This helper supports only an unauthenticated IPv4 SMTP relay on port 25")
    if not re.fullmatch(r"[A-Za-z0-9.!#%&'*+/=?^_{}|~-]+@[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)+", email):
        raise MailError("Supply one valid system mailbox, without display names or control characters")
    expected = {"FORGEJO_MAILER_HOST": host, "FORGEJO_MAILER_PORT": "25",
                "FORGEJO_MAILER_FROM": email, "FORGEJO_MAILER_PROTOCOL": "smtp",
                "FORGEJO_MAILER_ENABLED": "true"}
    if any(key in settings and settings[key] != value for key, value in expected.items()):
        raise MailError("Forgejo and shared relay settings disagree")
    return {"host": host, "port": port, "email": email}


def dump(value: object) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def forgejo_overlay(values: dict, settings: dict) -> dict:
    gitea = mapping(values.get("gitea", {}))
    config_envs = gitea.get("additionalConfigFromEnvs", [])
    if not isinstance(config_envs, list):
        raise MailError("Forgejo configuration environment entries are invalid")
    for item in config_envs:
        if not isinstance(item, dict):
            raise MailError("Forgejo configuration environment entry is invalid")
        name = item.get("name", "")
        if not isinstance(name, str) or name.upper().startswith(("FORGEJO__MAILER__", "GITEA__MAILER__")):
            raise MailError("Forgejo mailer is overridden by configuration environment; review that source first")
    config = mapping(gitea.get("config", {}))
    mapping(config.get("service", {}))
    mailer = copy.deepcopy(mapping(config.get("mailer", {})))
    # Changing a relay must not silently forward existing SMTP credentials to it,
    # or downgrade a deployment using mandatory TLS / client certificates.
    if any(mailer.get(key) for key in ("USER", "PASSWD", "PASSWD_URI", "USE_CLIENT_CERT", "FORCE_TRUST_SERVER_CERT")):
        raise MailError("Existing authenticated or custom-TLS Forgejo mail needs separate review")
    if mailer.get("PROTOCOL", "smtp") not in ("", "smtp"):
        raise MailError("A different Forgejo mail protocol is configured; no TLS downgrade is permitted")
    if mailer.get("ENVELOPE_FROM") not in (None, "", settings["email"]):
        raise MailError("A different Forgejo envelope sender is configured; review its routing first")
    mailer.update(ENABLED=True, PROTOCOL="smtp", SMTP_ADDR=settings["host"],
                  SMTP_PORT=settings["port"], FROM=settings["email"])
    return {"gitea": {"config": {"mailer": mailer, "service": {"ENABLE_NOTIFY_MAIL": True}}}}


def argocd_overlay(values: dict, settings: dict) -> dict:
    notifications = copy.deepcopy(mapping(values.get("notifications", {})))
    if mapping(notifications.get("cm", {})).get("create") is False:
        raise MailError("Argo CD notification ConfigMap is managed externally; review its existing owner")
    pod_labels = mapping(notifications.setdefault("podLabels", {}))
    sender_label = "platform.gitops/system-mail-sender"
    if sender_label in pod_labels and pod_labels[sender_label] != "argocd-notifications":
        raise MailError("Argo CD mail sender label conflicts with existing configuration")
    pod_labels[sender_label] = "argocd-notifications"
    notifiers = notifications.setdefault("notifiers", {})
    mapping(notifiers)
    service = dump({"host": settings["host"], "port": settings["port"],
                    "from": settings["email"], "insecure_skip_verify": False})
    existing = notifiers.get("service.email")
    if existing is not None and (not isinstance(existing, str) or document(existing) != document(service)):
        raise MailError("A different Argo CD email notifier already exists; it was not overwritten")
    notifiers["service.email"] = service
    template = dump({"email": {"subject": "[Argo CD] {{.app.metadata.name}} needs attention"},
                     "message": "Application: {{.app.metadata.name}}\nSync: {{.app.status.sync.status}}\n"
                                "Health: {{.app.status.health.status}}\n"
                                "Details: {{.context.argocdUrl}}/applications/{{.app.metadata.name}}\n"})
    templates = mapping(notifications.setdefault("templates", {}))
    key = "template.platform-mail-attention"
    if key in templates and templates[key] != template:
        raise MailError("A managed notification template conflicts with existing configuration")
    templates[key] = template
    triggers = mapping(notifications.setdefault("triggers", {}))
    conditions = ["app.status.operationState != nil && app.status.operationState.phase in ['Error', 'Failed']",
                  "app.status.health.status == 'Degraded'"]
    for name, condition in zip(MANAGED_TRIGGERS, conditions):
        body = dump([{"when": condition, "send": ["platform-mail-attention"]}])
        key = "trigger." + name
        if key in triggers and triggers[key] != body:
            raise MailError("A managed notification trigger conflicts with existing configuration")
        triggers[key] = body
    subscriptions = notifications.setdefault("subscriptions", [])
    if not isinstance(subscriptions, list):
        raise MailError("Existing notification subscriptions are not a list")
    subscription = {"recipients": ["email:" + settings["email"]], "triggers": MANAGED_TRIGGERS.copy()}
    # Refuse silently retaining an old shared mailbox under the managed triggers.
    for item in subscriptions:
        if not isinstance(item, dict):
            raise MailError("Existing notification subscription is invalid")
        item_triggers = item.get("triggers", [])
        if not isinstance(item_triggers, list):
            raise MailError("Existing subscription triggers are invalid")
        if set(MANAGED_TRIGGERS) & set(item_triggers) and item != subscription:
            raise MailError("Managed mail subscription already exists with different recipients")
    if subscription not in subscriptions:
        subscriptions.append(subscription)
    notifications["enabled"] = True
    return {"notifications": notifications}


def woodpecker_step() -> dict:
    return {"steps": {"system-mail": {
        "image": EMAIL_IMAGE,
        "settings": {
            "dsn": {"from_secret": "system_mail_dsn"},
            # Plugin 3.3.0 decodes PLUGIN_FROM as JSON. A nested from_secret
            # inside from.address would remain a JSON object, not an address.
            "from": {"from_secret": "system_mail_from"},
            "recipients": {"from_secret": "system_mail_to"}, "recipients_only": True,
            "content": {"subject": "[Woodpecker] {{ pipeline.status }}: {{ repo.full_name }}",
                        "body": "Pipeline status: {{ pipeline.status }}<br>Details: {{ pipeline.url }}"},
        },
        "when": {"status": ["failure"], "event": ["push", "tag", "manual"]},
    }}}


def smtp_policies(settings: dict) -> list[dict]:
    return [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
             "metadata": {"name": "platform-system-mail-relay", "namespace": namespace},
             "spec": {"podSelector": selector, "policyTypes": ["Egress"],
                      "egress": [{"to": [{"ipBlock": {"cidr": settings["host"] + "/32"}}],
                                  "ports": [{"protocol": "TCP", "port": settings["port"]}]}]}}
            for namespace, selector in (
                ("argocd", {"matchLabels": {"platform.gitops/system-mail-sender": "argocd-notifications"}}),
                ("woodpecker", {}),
            )]


def prepare(settings: dict, forgejo: dict, argocd: dict, *, woodpecker_plain_smtp: bool = False) -> dict[str, str]:
    if type(woodpecker_plain_smtp) is not bool:
        raise MailError("Woodpecker plain SMTP must be an explicit boolean choice")
    # Symfony Mailer otherwise negotiates STARTTLS when the relay advertises it.
    # This is a transport choice, not a certificate-verification bypass.
    dsn_options = ("auto_tls=false&" if woodpecker_plain_smtp else "") + "verify_peer=1"
    return {
        "forgejo-mail-values.yaml": dump(forgejo_overlay(forgejo, settings)),
        "argocd-mail-values.yaml": dump(argocd_overlay(argocd, settings)),
        "smtp-egress.yaml": yaml.safe_dump_all(smtp_policies(settings), sort_keys=False),
        "woodpecker-mail-step.yaml": dump(woodpecker_step()),
        # No SMTP password: these entries keep internal routing data out of pipelines.
        "woodpecker-mail-secrets.env": (
            f"system_mail_dsn=smtp://{settings['host']}:{settings['port']}?{dsn_options}\n"
            "system_mail_from=" + json.dumps({"address": settings["email"], "name": "Woodpecker"}) + "\n"
            f"system_mail_to={settings['email']}\n"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / "private/system-mail.env")
    parser.add_argument("--forgejo-values", type=Path, default=APPS / "forgejo/values.yaml")
    parser.add_argument("--argocd-values", type=Path, default=APPS / "argocd-ha/values.yaml")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "private/system-mail")
    parser.add_argument("--write", action="store_true", help="Write ignored local overlays only; no cluster changes")
    parser.add_argument("--woodpecker-plain-smtp", action="store_true",
                        help="Explicitly disable automatic TLS for Woodpecker's private relay; messages are unencrypted")
    args = parser.parse_args(argv)
    try:
        files = prepare(load_settings(args.env_file), document(read_text(args.forgejo_values)),
                        document(read_text(args.argocd_values)), woodpecker_plain_smtp=args.woodpecker_plain_smtp)
        paths = {name: private_output(args.output_dir / name) for name in files}
        # Never overwrite operator edits. Identical reruns are safe.
        for name, path in paths.items():
            if path.exists() and read_text(path) != files[name]:
                raise MailError("A prepared output differs; preserve it and select a new private output directory")
        if args.write:
            for name, path in paths.items():
                if not path.exists():
                    atomic_write_text(path, files[name])
        print(json.dumps({"mode": "prepared-private-overlays" if args.write else "plan-only",
                          "artifacts": list(files), "mail_authentication": "none",
                          "cluster_changed": False, "delivery_verified": False,
                          "woodpecker_plain_smtp": args.woodpecker_plain_smtp,
                          "woodpecker_requires_pipeline_and_secret_configuration": True}, indent=2))
        return 0
    except Exception:
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, MailError) else "Mail preparation failed; private details suppressed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
