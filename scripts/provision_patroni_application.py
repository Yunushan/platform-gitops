#!/usr/bin/env python3
"""Provision one declared application database on the reviewed Patroni primary."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

import materialize_patroni as materializer
from postgres_ha_policy import application_databases
from atomic_file import atomic_write_bytes


ADMIN = "platform_pg_admin"
ROLE_STATUS = """
SELECT shobj_description(r.oid, 'pg_authid'), r.rolcanlogin,
       NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
            OR r.rolbypassrls OR r.rolinherit)
       AND r.rolvaliduntil IS NULL
       AND NOT EXISTS (SELECT FROM pg_auth_members WHERE roleid = r.oid OR member = r.oid)
       AND NOT EXISTS (SELECT FROM pg_database WHERE datdba = r.oid AND datname <> %s),
       coalesce(r.rolpassword LIKE 'SCRAM-SHA-256$%%', false)
FROM pg_authid AS r WHERE r.rolname = %s
"""
DATABASE_STATUS = """
SELECT shobj_description(d.oid, 'pg_database'), pg_get_userbyid(d.datdba),
       d.datallowconn, NOT d.datistemplate,
       NOT EXISTS (
           SELECT FROM aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) AS acl
           WHERE acl.grantee <> d.datdba
       )
FROM pg_database AS d WHERE d.datname = %s
"""


def input_bytes(path, limit, *, private):
    materializer.directory(path.parent)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & (0o077 if private else 0o022)):
            raise ValueError("Untrusted PostgreSQL provisioning input")
        data = stream.read(limit + 1)
        if not data or len(data) > limit:
            raise ValueError("Untrusted PostgreSQL provisioning input length")
        return data


def password(path):
    value = input_bytes(path, 1024, private=True).decode("utf-8").removesuffix("\n").removesuffix("\r")
    if len(value) < 16 or not value.isprintable():
        raise ValueError("Use a nonempty private, high-entropy database password")
    return value


def require_primary(connection, plan, system_identifier):
    version = int(plan["postgres_version"].split(".")[0]) * 10000 + int(plan["postgres_version"].split(".")[1])
    replication = plan["replication"]
    row = connection.execute("""
        SELECT current_user, current_database(), current_setting('server_version_num')::integer,
               (SELECT system_identifier::text FROM pg_control_system()),
               pg_is_in_recovery(), current_setting('transaction_read_only'),
               current_setting('synchronous_commit'), current_setting('password_encryption'),
               (SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming' AND sync_state = 'quorum'),
               (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())
        """).fetchone()
    if (row[:8] != (ADMIN, "postgres", version, system_identifier, False, "off",
                   replication["synchronous_commit"], "scram-sha-256")
            or row[8] < replication["standbys"] or row[9] is not True or not connection.pgconn.used_password):
        raise ValueError("The selected original primary and synchronous quorum are required")


def require_application_hba(connection, app):
    allowed, denied = connection.execute("""
        SELECT EXISTS (
            SELECT FROM pg_hba_file_rules WHERE type = 'hostssl'
            AND "database" = ARRAY[%s]::text[] AND user_name = ARRAY[%s]::text[]
            AND auth_method = 'scram-sha-256' AND error IS NULL
        ), EXISTS (
            SELECT FROM pg_hba_file_rules WHERE type = 'host' AND %s = ANY(user_name)
            AND auth_method = 'reject' AND error IS NULL
        )
        """, (app["database"], app["owner"], app["owner"])).fetchone()
    if not allowed or not denied:
        raise ValueError("Install the reviewed Patroni application access rules before provisioning")


def confirm_replayed(connection, replication):
    position, = connection.execute("SELECT pg_current_wal_flush_lsn()").fetchone()
    deadline = time.monotonic() + 30
    # These column names are fixed SQL fragments selected by the validated policy.
    column = "replay_lsn" if replication["synchronous_commit"] == "remote_apply" else "flush_lsn"
    while True:
        count, = connection.execute(
            f"SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming' "
            f"AND sync_state = 'quorum' AND {column} >= %s::pg_lsn", (position,)
        ).fetchone()
        if count >= replication["standbys"]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("The reviewed synchronous quorum has not confirmed the application state")
        time.sleep(0.25)


def create_application(connection, plan, app, app_password, system_identifier):
    from psycopg import sql

    database, owner = app["database"], app["owner"]
    db_name, role_name = sql.Identifier(database), sql.Identifier(owner)
    marker = f"platform-gitops patroni application v1 {plan['cluster_id']} {database} {owner}"
    comment_role = sql.SQL("COMMENT ON ROLE {} IS {}").format(role_name, sql.Literal(marker))
    comment_database = sql.SQL("COMMENT ON DATABASE {} IS {}").format(db_name, sql.Literal(marker))
    role = connection.execute(ROLE_STATUS, (database, owner)).fetchone()
    db = connection.execute(DATABASE_STATUS, (database,)).fetchone()
    if (role is not None and (role[0] != marker or role[2] is not True or role[1] != role[3])
            or db is not None and (role is None or db[1] != owner or db[3] is not True)):
        raise ValueError("Existing application identities differ from the reviewed provisioning state")
    if role is not None and role[1]:
        if db != (marker, owner, True, True, True):
            raise ValueError("Existing application database ownership or privileges differ")
        return False
    if db is not None and db != (None, owner, False, True, False):
        raise ValueError("Existing database differs from the incomplete provisioning state")
    if role is None:
        require_primary(connection, plan, system_identifier)
        with connection.transaction():
            connection.execute(sql.SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(role_name))
            connection.execute(comment_role)
    # PostgreSQL runs CREATE DATABASE outside a transaction. Keep the database
    # closed until the final transaction activates its restricted ACL and login.
    if db is None:
        require_primary(connection, plan, system_identifier)
        connection.execute(sql.SQL(
            "CREATE DATABASE {} OWNER {} TEMPLATE template0 ALLOW_CONNECTIONS false"
        ).format(db_name, role_name))
    require_primary(connection, plan, system_identifier)
    verifier = connection.pgconn.encrypt_password(app_password.encode("utf-8"), owner.encode("ascii"),
                                                   b"scram-sha-256").decode("ascii")
    with connection.transaction():
        connection.execute("SET LOCAL log_statement = 'none'")
        connection.execute("SET LOCAL log_min_error_statement = 'panic'")
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db_name))
        connection.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(db_name))
        connection.execute(comment_database)
        connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {} LOGIN").format(role_name, sql.Literal(verifier)))
    return True


def provision(plan, app, member, system_identifier, inputs_dir, application_secrets):
    import psycopg

    if not isinstance(system_identifier, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", system_identifier) or int(system_identifier) >= 2 ** 64:
        raise ValueError("Pin the reviewed PostgreSQL system identifier")
    if any(name.startswith("PG") for name in os.environ):
        raise ValueError("Clear libpq environment settings before connecting")
    ca = input_bytes(inputs_dir / "postgres-ca.crt", 256 * 1024, private=False)
    if hashlib.sha256(ca).hexdigest() != plan["tls"]["ca_sha256"]["postgres"]:
        raise ValueError("PostgreSQL trust anchor differs from the contract")
    admin_password = password(inputs_dir / "superuser-password")
    app_password = password(application_secrets / app["secret"] / "password")
    if app_password == admin_password:
        raise ValueError("Use a dedicated application password")
    settings = dict(host=member["host"], hostaddr=member["address"], port=plan["ports"]["sql"],
                    sslmode="verify-full", gssencmode="disable", connect_timeout=5, tcp_user_timeout=8000,
                    options="-c search_path=pg_catalog -c statement_timeout=30000 -c lock_timeout=5000",
                    autocommit=True)
    with tempfile.TemporaryDirectory(prefix="platform-postgres-application-") as tmp:
        ca_path = Path(tmp) / "ca.crt"
        atomic_write_bytes(ca_path, ca, replace=False)
        settings["sslrootcert"] = str(ca_path)
        with psycopg.connect(**settings, dbname="postgres", user=ADMIN, password=admin_password) as connection:
            require_primary(connection, plan, system_identifier)
            acquired, = connection.execute("SELECT pg_try_advisory_lock(57820, 4126)").fetchone()
            if not acquired:
                raise ValueError("Another application provisioning session holds the database lock")
            require_application_hba(connection, app)
            changed = create_application(connection, plan, app, app_password, system_identifier)
            confirm_replayed(connection, plan["replication"])
            require_primary(connection, plan, system_identifier)
            with psycopg.connect(**settings, dbname=app["database"], user=app["owner"], password=app_password) as client:
                row = client.execute("""
                    SELECT current_user, current_database(), has_schema_privilege(current_user, 'public', 'CREATE'),
                           (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())
                    """).fetchone()
                if row != (app["owner"], app["database"], True, True) or not client.pgconn.used_password:
                    raise ValueError("Application login or schema permissions differ")
    return {"application_provisioned": True, "changed": changed, "tls_login_verified": True,
            "synchronous_quorum_confirmed": True, "full_stack_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--member", required=True)
    parser.add_argument("--database", help="Declared target database. Defaults to the bootstrap database.")
    parser.add_argument("--system-identifier")
    parser.add_argument("--inputs-dir", type=Path)
    parser.add_argument("--application-secrets-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        bundle = Path(__file__).resolve().parent
        contract, _ = materializer.load(bundle, args.contract_sha256, args.member)
        if Path(__file__).name not in contract["files_sha256"]:
            raise ValueError("Require a package that includes this provisioning helper")
        plan = contract["plan"]
        target = args.database if args.database is not None else plan["bootstrap"]["database"]
        app = next(item for item in application_databases(plan) if item["database"] == target)
        member = next(item for item in plan["members"] if item["name"] == args.member)
        result = {"plan_verified": True, "application_provisioned": False, "full_stack_verified": False}
        if args.apply:
            if not args.system_identifier or not args.inputs_dir or not args.application_secrets_dir:
                raise ValueError("Supply original database identity and private input paths")
            result.update(provision(plan, app, member, args.system_identifier,
                                    args.inputs_dir, args.application_secrets_dir))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print("Application database provisioning refused or incomplete. Private details were suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
