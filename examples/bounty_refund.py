#!/usr/bin/env python3
"""Durable bounty/refund ledger backed by the authenticated local Outbox API."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(base: str, token: str, method: str, path: str, body=None):
    data = None if body is None else canonical(body).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=900) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            try:
                payload = json.load(error)
            except (ValueError, UnicodeError):
                payload = {"error": "HTTP response did not contain JSON"}
            return error.code, payload


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate intent JSON key: " + key)
        result[key] = value
    return result


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_ledger(path: Path) -> sqlite3.Connection:
    if str(path) == ":memory:":
        raise ValueError("a durable ledger file is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if (version not in (0, 1) or application not in (0, 0x424F5842)
            or (application == 0 and tables)):
        connection.close()
        raise ValueError("file is not an empty database or supported business ledger")
    connection.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS business_payments (
          payment_id TEXT PRIMARY KEY,
          intent_json TEXT NOT NULL,
          intent_sha256 TEXT NOT NULL,
          required_confirmations INTEGER NOT NULL CHECK(required_confirmations BETWEEN 1 AND 100),
          outbox_state_json TEXT,
          last_http_status INTEGER,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY,
          payment_id TEXT NOT NULL REFERENCES business_payments(payment_id),
          operation TEXT NOT NULL,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          http_status INTEGER,
          response_json TEXT,
          error TEXT
        );
        CREATE TRIGGER IF NOT EXISTS business_identity_immutable
        BEFORE UPDATE OF payment_id,intent_json,intent_sha256,required_confirmations ON business_payments
        WHEN NEW.payment_id != OLD.payment_id OR NEW.intent_json != OLD.intent_json
          OR NEW.intent_sha256 != OLD.intent_sha256
          OR NEW.required_confirmations != OLD.required_confirmations
        BEGIN SELECT RAISE(ABORT,'business identity and intent are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS business_identity_retained
        BEFORE DELETE ON business_payments
        BEGIN SELECT RAISE(ABORT,'business payment identity must be retained'); END;
        PRAGMA application_id=1112496194;
        PRAGMA user_version=1;
        COMMIT;
    """)
    return connection


def bind_or_get(connection, payment_id, intent=None, confirmations=None):
    """Commit the business binding before any HTTP call, including health."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM business_payments WHERE payment_id=?", (payment_id,)).fetchone()
        if row is None:
            if intent is None:
                raise ValueError("business ID is absent from this ledger; register its exact intent first")
            text = canonical(intent)
            timestamp = now()
            connection.execute("""INSERT INTO business_payments
                (payment_id,intent_json,intent_sha256,required_confirmations,created_at,updated_at)
                VALUES (?,?,?,?,?,?)""", (payment_id, text, hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                        confirmations or 3, timestamp, timestamp))
        else:
            if intent is not None and canonical(intent) != row["intent_json"]:
                raise ValueError("business ID is already bound to a different exact intent; keep the original ID and investigate")
            if confirmations is not None and confirmations != row["required_confirmations"]:
                raise ValueError("business ID is already bound to a different confirmation threshold")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return connection.execute("SELECT * FROM business_payments WHERE payment_id=?", (payment_id,)).fetchone()


def is_paid(payment, required: int) -> bool:
    if not isinstance(payment, dict):
        return False
    observation = payment.get("chain_observation")
    if not isinstance(observation, dict) or observation.get("state") != "confirmed":
        return False
    count = observation.get("confirmations")
    threshold = observation.get("required_confirmations")
    return (type(count) is int and type(threshold) is int and threshold >= 1
            and count >= max(required, threshold)
            and isinstance(payment.get("txid"), str) and bool(payment["txid"])
            and observation.get("txid") == payment["txid"])


def finish_attempt(connection, attempt_id, payment_id, status, payload, error):
    approved = connection.execute("SELECT intent_json FROM business_payments WHERE payment_id=?",
                                  (payment_id,)).fetchone()[0]
    good = (status in (200, 202) and isinstance(payload, dict)
            and payload.get("id") == payment_id and isinstance(payload.get("stage"), str)
            and canonical(payload.get("intent")) == approved)
    if status in (200, 202) and not good:
        error = "service response did not match the business ID and exact approved intent"
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""UPDATE attempts SET completed_at=?,http_status=?,response_json=?,error=? WHERE id=?""",
                           (now(), status, canonical(payload) if payload is not None else None, error, attempt_id))
        if good:
            connection.execute("""UPDATE business_payments SET outbox_state_json=?,last_http_status=?,
                last_error=NULL,updated_at=? WHERE payment_id=?""", (canonical(payload), status, now(), payment_id))
        else:
            # Keep the binding and last observed state. An error never creates a replacement payment.
            connection.execute("""UPDATE business_payments SET last_http_status=?,last_error=?,updated_at=?
                WHERE payment_id=?""", (status, error or "service request failed", now(), payment_id))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return good, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:17840")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=Path("runtime/business.db"))
    parser.add_argument("operation", choices=("register", "query", "pay", "refresh"))
    parser.add_argument("--kind", required=True, choices=("bounty", "refund"))
    parser.add_argument("--business-id", required=True, help="Original bounty or purchase identifier on every invocation")
    parser.add_argument("--intent", type=Path, help="Required only for register")
    parser.add_argument("--confirmations", type=int, help="Immutable settlement threshold on first registration (default 3)")
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    try:
        local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        parsed.port
    except ValueError:
        local = False
    if parsed.scheme != "http" or not local or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        parser.error("--url must be an HTTP loopback origin")
    payment_id = args.kind + ":" + args.business_id
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", payment_id):
        parser.error("business ID is too long or contains unsupported characters")
    if (args.operation == "register") != (args.intent is not None):
        parser.error("--intent is required only for register")
    if args.confirmations is not None and not 1 <= args.confirmations <= 100:
        parser.error("--confirmations must be between 1 and 100")
    intent = None
    if args.intent is not None:
        intent = json.loads(args.intent.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        if not isinstance(intent, dict):
            parser.error("intent must be a JSON object")
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        parser.error("token file is empty")
    connection = open_ledger(args.ledger)
    try:
        row = bind_or_get(connection, payment_id, intent, args.confirmations)
        attempt = connection.execute("INSERT INTO attempts(payment_id,operation,started_at) VALUES (?,?,?)",
                                     (payment_id, args.operation, now())).lastrowid
        health = None
        status = None
        payload = None
        failure = None
        try:
            base = args.url.rstrip("/")
            health_status, health = request(base, token, "GET", "/health")
            if health_status != 200:
                status, payload = health_status, health
                failure = "service health request failed"
            else:
                path = "/payments/" + payment_id
                method, body = "GET", None
                if args.operation == "register":
                    method, body = "PUT", json.loads(row["intent_json"])
                elif args.operation == "pay":
                    method, path = "POST", path + "/retry"
                elif args.operation == "refresh":
                    method, path = "POST", path + "/refresh"
                    body = {"confirmations": row["required_confirmations"]}
                status, payload = request(base, token, method, path, body)
                if status not in (200, 202):
                    failure = "service request failed; preserve this business ID and ledger"
        except (OSError, ValueError, urllib.error.URLError) as error:
            failure = "request outcome uncertain: " + str(error)
        good, failure = finish_attempt(connection, attempt, payment_id, status, payload, failure)
        result = {
            "payment_id": payment_id, "http_status": status, "service": health,
            "payment": payload, "paid": good and is_paid(payload, row["required_confirmations"]),
            "required_confirmations": row["required_confirmations"],
            "ledger": str(args.ledger),
        }
        if failure:
            result["error"] = failure
            result["last_observed_payment"] = json.loads(row["outbox_state_json"]) if row["outbox_state_json"] else None
        print(json.dumps(result, indent=2))
        return 0 if good else 1
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sqlite3.Error, urllib.error.URLError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
