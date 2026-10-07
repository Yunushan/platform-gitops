#!/usr/bin/env python3
"""Test private, mail-only preparation with synthetic routing data."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit
from unittest import mock

import configure_system_mail as mail


SETTINGS = {"host": "203.0.113.25", "port": 25, "email": "platform@example.test"}


def rejects(action) -> None:
    try:
        action()
    except (mail.MailError, ValueError):
        return
    raise AssertionError("Unsafe mail configuration was accepted")


def tests() -> None:
    forgejo = {"persistence": {"size": "100Gi", "storageClass": "example-class"},
               "gitea": {"admin": {"username": "existing-admin"},
                         "config": {"database": {"DB_TYPE": "postgres"},
                                    "mailer": {"SUBJECT_PREFIX": "[Forgejo]"}}}}
    argocd = {"configs": {"cm": {"admin.enabled": False}},
              "notifications": {"resources": {"requests": {"memory": "64Mi"}},
                                "notifiers": {"service.webhook.example": "url: https://example.test\n"},
                                "subscriptions": [{"recipients": ["webhook:example"], "triggers": ["example"]}]}}
    originals = copy.deepcopy((forgejo, argocd))
    files = mail.prepare(SETTINGS, forgejo, argocd)
    assert (forgejo, argocd) == originals
    f_overlay = mail.document(files["forgejo-mail-values.yaml"])
    assert set(f_overlay) == {"gitea"}
    assert set(f_overlay["gitea"]) == {"config"}
    assert set(f_overlay["gitea"]["config"]) == {"mailer", "service"}
    assert f_overlay["gitea"]["config"]["mailer"]["SUBJECT_PREFIX"] == "[Forgejo]"
    assert f_overlay["gitea"]["config"]["mailer"]["FROM"] == SETTINGS["email"]
    a_overlay = mail.document(files["argocd-mail-values.yaml"])
    assert set(a_overlay) == {"notifications"}
    n = a_overlay["notifications"]
    assert n["resources"] == argocd["notifications"]["resources"]
    assert n["podLabels"]["platform.gitops/system-mail-sender"] == "argocd-notifications"
    assert n["subscriptions"][0] == argocd["notifications"]["subscriptions"][0]
    assert len(n["subscriptions"]) == 2
    notifier = mail.document(n["notifiers"]["service.email"])
    assert notifier == {"host": SETTINGS["host"], "port": 25, "from": SETTINGS["email"], "insecure_skip_verify": False}
    assert "password" not in notifier and "username" not in notifier
    assert mail.argocd_overlay(a_overlay, SETTINGS) == a_overlay
    assert mail.forgejo_overlay(f_overlay, SETTINGS) == f_overlay
    for key in ("USER", "PASSWD", "PASSWD_URI", "USE_CLIENT_CERT", "FORCE_TRUST_SERVER_CERT"):
        rejects(lambda key=key: mail.forgejo_overlay({"gitea": {"config": {"mailer": {key: "example"}}}}, SETTINGS))
    rejects(lambda: mail.forgejo_overlay({"gitea": {"config": {"mailer": {"PROTOCOL": "smtp+starttls"}}}}, SETTINGS))
    rejects(lambda: mail.forgejo_overlay({"gitea": {"config": {"mailer": {
        "ENVELOPE_FROM": "other@example.test"}}}}, SETTINGS))
    rejects(lambda: mail.forgejo_overlay({"gitea": {"additionalConfigFromEnvs": [
        {"name": "FORGEJO__MAILER__SMTP_ADDR", "value": "other.example.test"}]}}, SETTINGS))
    for parent, field in (("gitea", "additionalConfigFromEnvs"), ("deployment", "env")):
        for prefix in ("FORGEJO__MAILER__", "GITEA__MAILER__"):
            for key in ("SMTP_ADDR", "PROTOCOL", "USER", "PASSWD"):
                rejects(lambda parent=parent, field=field, prefix=prefix, key=key:
                        mail.forgejo_overlay({parent: {field: [{"name": prefix + key,
                            "valueFrom": {"secretKeyRef": {"name": "example-mail", "key": "example"}}}]}}, SETTINGS))
        for entries in (None, {}, [None], [{"name": None}]):
            rejects(lambda parent=parent, field=field, entries=entries:
                    mail.forgejo_overlay({parent: {field: entries}}, SETTINGS))
        existing_env = {parent: {field: [{"name": "FORGEJO__DATABASE__DB_TYPE", "value": "postgres"}]}}
        before = copy.deepcopy(existing_env)
        assert mail.forgejo_overlay(existing_env, SETTINGS)["gitea"]["config"]["mailer"]["FROM"] == SETTINGS["email"]
        assert existing_env == before
    rejects(lambda: mail.argocd_overlay({"notifications": {"cm": {"create": False}}}, SETTINGS))
    rejects(lambda: mail.argocd_overlay({"notifications": {"podLabels": {
        "platform.gitops/system-mail-sender": "other"}}}, SETTINGS))
    rejects(lambda: mail.argocd_overlay({"notifications": {"notifiers": {"service.email": "host: other.example.test\n"}}}, SETTINGS))
    rejects(lambda: mail.argocd_overlay({"notifications": {"templates": {"template.platform-mail-attention": "other"}}}, SETTINGS))
    rejects(lambda: mail.argocd_overlay({"notifications": {"subscriptions": [
        {"recipients": ["email:other@example.test"], "triggers": mail.MANAGED_TRIGGERS}]}}, SETTINGS))
    rejects(lambda: mail.document("x: 1\nx: 2\n"))
    rejects(lambda: mail.document("x: &x {y: 1}\nz: *x\n"))
    policies = mail.loads_strict_yaml_all(files["smtp-egress.yaml"])
    assert {p["metadata"]["namespace"] for p in policies} == {"argocd", "woodpecker"}
    assert policies[0]["spec"]["podSelector"]["matchLabels"] == n["podLabels"]
    assert policies[1]["spec"]["podSelector"] == {"matchLabels": {"woodpecker-ci.org/step": "system-mail"}}
    assert policies[1]["spec"]["podSelector"]["matchLabels"]["woodpecker-ci.org/step"] in (
        mail.document(files["woodpecker-mail-step.yaml"])["steps"]
    )
    for p in policies:
        assert p["spec"]["egress"] == [{"to": [{"ipBlock": {"cidr": SETTINGS["host"] + "/32"}}],
                                        "ports": [{"protocol": "TCP", "port": 25}]}]
    step = mail.document(files["woodpecker-mail-step.yaml"])["steps"]["system-mail"]
    assert "@sha256:" in step["image"]
    assert step["image"] == mail.EMAIL_IMAGE
    # Woodpecker 3.16's secret-image validator rejects dots in tag strings.
    assert step["image"].split("@", 1)[0] == "deblan/woodpecker-email"
    assert step["settings"]["from"] == {"from_secret": "system_mail_from"}
    assert step["settings"]["recipients_only"] is True
    assert step["when"]["status"] == ["failure"]
    assert "pull_request" not in step["when"]["event"]
    assert "attachments" not in step["settings"] and "commands" not in step
    secret_values = dict(line.split("=", 1) for line in files["woodpecker-mail-secrets.env"].splitlines())
    assert json.loads(secret_values["system_mail_from"])["address"] == SETTINGS["email"]
    assert secret_values["system_mail_to"] == SETTINGS["email"]
    assert "verify_peer=1" in secret_values["system_mail_dsn"]
    default_dsn = urlsplit(secret_values["system_mail_dsn"])
    assert parse_qs(default_dsn.query) == {"verify_peer": ["1"]}
    plain_files = mail.prepare(SETTINGS, forgejo, argocd, woodpecker_plain_smtp=True)
    plain_secrets = dict(line.split("=", 1) for line in plain_files["woodpecker-mail-secrets.env"].splitlines())
    plain_dsn = urlsplit(plain_secrets["system_mail_dsn"])
    assert plain_dsn.scheme == "smtp" and plain_dsn.port == 25
    assert plain_dsn.username is None and plain_dsn.password is None
    assert parse_qs(plain_dsn.query) == {"auto_tls": ["false"], "verify_peer": ["1"]}
    assert plain_secrets["system_mail_from"] == secret_values["system_mail_from"]
    assert plain_secrets["system_mail_to"] == secret_values["system_mail_to"]
    assert {name: text for name, text in plain_files.items() if name != "woodpecker-mail-secrets.env"} == {
        name: text for name, text in files.items() if name != "woodpecker-mail-secrets.env"}
    rejects(lambda: mail.prepare(SETTINGS, forgejo, argocd, woodpecker_plain_smtp="false"))
    assert SETTINGS["host"] not in files["woodpecker-mail-step.yaml"]
    assert SETTINGS["email"] not in files["woodpecker-mail-step.yaml"]

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        env = root / "system-mail.env"
        env.write_text("PLATFORM_SMTP_HOST=203.0.113.25\nPLATFORM_SYSTEM_EMAIL=platform@example.test\n", encoding="utf-8")
        assert mail.load_settings(env) == SETTINGS
        for invalid in (
            "PLATFORM_SMTP_PORT=587", "PLATFORM_SMTP_HOST=127.0.0.1", "PLATFORM_SYSTEM_EMAIL=bad",
            "PLATFORM_SYSTEM_EMAIL=$(example)", "OTHER=value", "FORGEJO_MAILER_HOST=203.0.113.26",
        ):
            env.write_text("PLATFORM_SMTP_HOST=203.0.113.25\nPLATFORM_SYSTEM_EMAIL=platform@example.test\n" + invalid + "\n", encoding="utf-8")
            rejects(lambda: mail.load_settings(env))
        output = root / "private/mail/values.yaml"
        def git_result(args, **kwargs):
            return mock.Mock(returncode=0 if args[3] == "check-ignore" else 1)
        with mock.patch.object(mail, "run_bounded", side_effect=git_result):
            assert mail.private_output(output, root) == output
            rejects(lambda: mail.private_output(root / "tracked.yaml", root))
        with mock.patch.object(mail, "run_bounded", return_value=mock.Mock(returncode=0)):
            rejects(lambda: mail.private_output(output, root))
        with mock.patch.object(mail, "run_bounded", return_value=mock.Mock(returncode=1)):
            rejects(lambda: mail.private_output(output, root))
        env.write_text("PLATFORM_SMTP_HOST=203.0.113.25\nPLATFORM_SYSTEM_EMAIL=platform@example.test\n", encoding="utf-8")
        output_dir = root / "private/mail"
        forgejo_values, argocd_values = root / "forgejo-values.yaml", root / "argocd-values.yaml"
        forgejo_values.write_text(mail.dump(forgejo), encoding="utf-8")
        argocd_values.write_text(mail.dump(argocd), encoding="utf-8")
        input_arguments = ["--env-file", str(env), "--forgejo-values", str(forgejo_values),
                           "--argocd-values", str(argocd_values)]
        arguments = input_arguments + ["--output-dir", str(output_dir)]
        # CLI tests write only temp fixtures; never actual private deployment files.
        with mock.patch.object(mail, "private_output", side_effect=lambda path: path):
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                assert mail.main(arguments) == 0
                assert not output_dir.exists()
                assert mail.main(arguments + ["--write"]) == 0
                assert mail.main(arguments + ["--write"]) == 0
            assert SETTINGS["host"] not in capture.getvalue() and SETTINGS["email"] not in capture.getvalue()
            assert all((output_dir / name).exists() for name in files)
            # Changing transport cannot overwrite prepared operator files.
            with contextlib.redirect_stderr(io.StringIO()):
                assert mail.main(arguments + ["--write", "--woodpecker-plain-smtp"]) == 1
            assert (output_dir / "woodpecker-mail-secrets.env").read_text(encoding="utf-8") == files["woodpecker-mail-secrets.env"]
            plain_dir = root / "private/plain-mail"
            plain_arguments = input_arguments + ["--output-dir", str(plain_dir), "--woodpecker-plain-smtp"]
            plain_capture = io.StringIO()
            with contextlib.redirect_stdout(plain_capture):
                assert mail.main(plain_arguments) == 0
                assert not plain_dir.exists()
                assert mail.main(plain_arguments + ["--write"]) == 0
            assert (plain_dir / "woodpecker-mail-secrets.env").read_text(encoding="utf-8") == plain_files["woodpecker-mail-secrets.env"]
            assert SETTINGS["host"] not in plain_capture.getvalue() and SETTINGS["email"] not in plain_capture.getvalue()
            plain_report, _ = json.JSONDecoder().raw_decode(plain_capture.getvalue())
            assert plain_report["woodpecker_plain_smtp"] is True
            assert plain_report["cluster_changed"] is False
            conflict = output_dir / "argocd-mail-values.yaml"
            conflict.write_text("operator edit\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                assert mail.main(arguments + ["--write"]) == 1
            assert conflict.read_text(encoding="utf-8") == "operator edit\n"
    print("Private system mail preparation tests passed.")


if __name__ == "__main__":
    tests()
