#!/usr/bin/env python3
"""Local SQLite/process-crash outbox probe. No Zcash, keys, RPC, or network."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

STAGES = ("create", "prove", "sign", "extract")
CRASH_EXIT = 91
PAYLOAD = {
    "network": "fake-regtest",
    "recipients": [{"address": "FAKE-recipient", "amount_zat": 100_000, "memo_hex": ""}],
    "fee_zat": 10_000,
    "expiry_height": 200,
}


def encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def canonical(payload):
    """Narrow fixture contract, not a Zcash address/ZIP validation implementation."""
    if set(payload) != {"network", "recipients", "fee_zat", "expiry_height"}:
        raise ValueError("unexpected or missing payment fields")
    if payload["network"] != "fake-regtest":
        raise ValueError("this probe accepts only FAKE network data")
    if not isinstance(payload["recipients"], list) or not payload["recipients"]:
        raise ValueError("recipients must be nonempty")
    for recipient in payload["recipients"]:
        if set(recipient) != {"address", "amount_zat", "memo_hex"}:
            raise ValueError("unexpected or missing recipient fields")
        if not isinstance(recipient["address"], str) or not recipient["address"].startswith("FAKE-"):
            raise ValueError("only FAKE addresses allowed")
        if type(recipient["amount_zat"]) is not int or recipient["amount_zat"] <= 0:
            raise ValueError("amount must be positive integer base units")
        if not isinstance(recipient["memo_hex"], str):
            raise ValueError("memo must be hex text")
        if bytes.fromhex(recipient["memo_hex"]).hex() != recipient["memo_hex"]:
            raise ValueError("memo must be canonical lowercase hex without whitespace")
    for field in ("fee_zat", "expiry_height"):
        if type(payload[field]) is not int or payload[field] < 0:
            raise ValueError(field + " must be nonnegative integer")
    return encode(payload).decode()


def digest(raw):
    # A FIXTURE byte identity only; this is NOT Zcash's transaction ID algorithm.
    return hashlib.sha256(raw).hexdigest()


def db(path):
    c = sqlite3.connect(path, isolation_level=None, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA synchronous=FULL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


@contextlib.contextmanager
def transaction(c):
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise


def initialize(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    schemas = {
        "outbox.db": """
        CREATE TABLE IF NOT EXISTS intents (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, policy TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'PENDING', completed INTEGER NOT NULL DEFAULT -1,
          active_stage TEXT, owner TEXT, artifact BLOB, raw BLOB, raw_hash TEXT,
          receipt TEXT, reason TEXT,
          CHECK ((raw IS NULL) = (raw_hash IS NULL)));
        CREATE TABLE IF NOT EXISTS pipeline_lock (
          id INTEGER PRIMARY KEY CHECK(id=1), intent TEXT, owner TEXT);
        INSERT OR IGNORE INTO pipeline_lock(id) VALUES (1);
        CREATE TABLE IF NOT EXISTS valid_commit_guard (id INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO valid_commit_guard VALUES (1);
        CREATE TABLE IF NOT EXISTS commit_guard (
          id INTEGER REFERENCES valid_commit_guard(id) DEFERRABLE INITIALLY DEFERRED);
        """,
        "wallet.db": """
        CREATE TABLE IF NOT EXISTS reserved (note INTEGER PRIMARY KEY, intent TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL,
          input BLOB NOT NULL, output BLOB NOT NULL);
        """,
        "chain.db": """
        CREATE TABLE IF NOT EXISTS attempts (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, raw BLOB NOT NULL, byte_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS accepted (
          byte_id TEXT PRIMARY KEY, raw BLOB NOT NULL, note INTEGER UNIQUE NOT NULL,
          intent TEXT NOT NULL, amount_zat INTEGER NOT NULL);
        """,
    }
    for name, schema in schemas.items():
        with contextlib.closing(db(root / name)) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(schema)


def row(root, intent="invoice-1"):
    with contextlib.closing(db(Path(root) / "outbox.db")) as c:
        return c.execute("SELECT * FROM intents WHERE id=?", (intent,)).fetchone()


def submit(root, intent, payload, policy="no_broadcast", crash=None):
    if not isinstance(intent, str) or not intent or len(intent) > 200:
        raise ValueError("invalid business intent ID")
    if policy not in {"no_broadcast", "unknown"}:
        raise ValueError("unknown adapter policy")
    value = canonical(payload)
    with contextlib.closing(db(Path(root) / "outbox.db")) as c, transaction(c):
        c.execute("INSERT OR IGNORE INTO intents(id,payload,policy) VALUES (?,?,?)",
                  (intent, value, policy))
        old = c.execute("SELECT payload,policy FROM intents WHERE id=?", (intent,)).fetchone()
        if old["payload"] != value or old["policy"] != policy:
            raise ValueError("intent ID already bound to another payload or adapter policy")
        hit("intent.before_commit", crash)
    hit("intent.after_commit", crash)


def hit(point, crash):
    if point == crash:
        os._exit(CRASH_EXIT)  # Real abrupt child death: no Python cleanup/close/flush.


class LostResponse(Exception):
    pass


class FakeWallet:
    """Every operation is local and explicitly INCAPABLE OF BROADCASTING.

    Nonce changes make re-extraction observably different. There are no keys,
    signatures, valid transactions, reservations, Zcash libraries or RPCs here.
    """
    def __init__(self, root, crash=None, lost=None, pause=0):
        self.root, self.crash, self.lost, self.pause = Path(root), crash, lost, pause

    def call(self, stage, input_bytes):
        if self.pause and stage == "create":
            time.sleep(self.pause)
        with contextlib.closing(db(self.root / "wallet.db")) as c, transaction(c):
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM calls").fetchone()[0]
            if stage == "create":
                # Proposals do not reserve inputs. Concurrent creates would choose the same note.
                reserved = {x[0] for x in c.execute("SELECT note FROM reserved")}
                note = 1
                while note in reserved:
                    note += 1
                data = {"intent": json.loads(input_bytes)["intent"],
                        "payload": json.loads(input_bytes)["payload"], "note": note}
            else:
                data = json.loads(input_bytes)
            if stage == "extract":
                previous = c.execute("SELECT intent FROM reserved WHERE note=?", (data["note"],)).fetchone()
                if previous is not None and previous[0] != data["intent"]:
                    raise RuntimeError("fake note reservation conflict")
                c.execute("INSERT OR IGNORE INTO reserved VALUES (?,?)", (data["note"], data["intent"]))
            data[stage + "_nonce"] = seq
            output = encode(data)
            c.execute("INSERT INTO calls(stage,input,output) VALUES (?,?,?)",
                      (stage, input_bytes, output))
        hit(stage + ".after_effect", self.crash)
        if self.lost == stage:
            raise LostResponse(stage + " response lost after fake operation completed")
        return output


class FakeBroadcaster:
    """Durable separate toy ledger, de-duplicates bytes and forbids double spending."""
    def __init__(self, root, crash=None, lost=None):
        self.root, self.crash, self.lost = Path(root), crash, lost

    def send(self, raw):
        byte_id, data = digest(raw), json.loads(raw)
        with contextlib.closing(db(self.root / "chain.db")) as c, transaction(c):
            c.execute("INSERT INTO attempts(raw,byte_id) VALUES (?,?)", (raw, byte_id))
            existing = c.execute("SELECT byte_id FROM accepted WHERE note=?", (data["note"],)).fetchone()
            if existing is not None and existing[0] != byte_id:
                raise RuntimeError("fake conflicting spend")
            c.execute("INSERT OR IGNORE INTO accepted VALUES (?,?,?,?,?)",
                      (byte_id, raw, data["note"], data["intent"],
                       sum(x["amount_zat"] for x in data["payload"]["recipients"])))
        hit("broadcast.after_effect", self.crash)
        if self.lost == "broadcast":
            raise LostResponse("broadcast response lost after fake acceptance")
        return byte_id


def set_halted(root, intent, reason):
    with contextlib.closing(db(Path(root) / "outbox.db")) as c, transaction(c):
        c.execute("UPDATE intents SET state='HALTED', owner=NULL, reason=? WHERE id=?",
                  (reason, intent))


def guarded_update(c, intent, owner, assignments, args=()):
    changed = c.execute("UPDATE intents SET " + assignments + " WHERE id=? AND owner=?",
                        (*args, intent, owner)).rowcount
    if changed != 1:
        raise RuntimeError("ownership fence lost; no side effect permitted")


def drive(root, intent="invoice-1", crash=None, lost=None,
          commit_fail=False, height=100, reorg=False, pause=0):
    root, owner = Path(root), str(uuid.uuid4())
    wallet = FakeWallet(root, crash, lost, pause)
    hit("before_claim", crash)
    with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
        item = c.execute("SELECT * FROM intents WHERE id=?", (intent,)).fetchone()
        if item is None:
            raise KeyError(intent)
        if item["state"] in {"BUILDING", "SENDING"}:
            return "BUSY"  # Never expire or steal a live build claim.
        if item["state"] == "HALTED":
            return "HALTED"
        if reorg:
            c.execute("UPDATE intents SET state='HALTED', reason='reorg ambiguity' WHERE id=?", (intent,))
            return "HALTED"
        if item["state"] == "ACK":
            return "ACK"  # Acceptance receipt is NOT settlement/finality.
        if height >= json.loads(item["payload"])["expiry_height"]:
            c.execute("UPDATE intents SET state='HALTED', reason='expired or at expiry boundary' WHERE id=?", (intent,))
            return "HALTED"
        if item["state"] == "PENDING":
            pipeline = c.execute("SELECT * FROM pipeline_lock WHERE id=1").fetchone()
            if pipeline["intent"] is not None and pipeline["intent"] != intent:
                return "BUSY_WALLET"
            c.execute("UPDATE pipeline_lock SET intent=?,owner=? WHERE id=1", (intent, owner))
            c.execute("UPDATE intents SET state='BUILDING', owner=? WHERE id=?", (owner, intent))
    hit("after_claim", crash)

    item = row(root, intent)
    if item["state"] == "BUILDING":
        for index in range(item["completed"] + 1, len(STAGES)):
            stage = STAGES[index]
            hit(stage + ".before_fence", crash)
            with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
                guarded_update(c, intent, owner, "active_stage=?", (stage,))
            hit(stage + ".after_fence", crash)
            item = row(root, intent)
            input_bytes = (encode({"intent": intent, "payload": json.loads(item["payload"])})
                           if index == 0 else item["artifact"])
            try:
                output = wallet.call(stage, input_bytes)
                hit(stage + ".after_response", crash)
                with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
                    if stage == "extract":
                        guarded_update(c, intent, owner,
                                       "raw=?,raw_hash=?,completed=?,artifact=?,active_stage=NULL,state='READY',owner=NULL",
                                       (output, digest(output), index, output))
                        released = c.execute("UPDATE pipeline_lock SET intent=NULL,owner=NULL WHERE id=1 AND intent=? AND owner=?",
                                             (intent, owner)).rowcount
                        if released != 1:
                            raise RuntimeError("wallet-wide pipeline ownership lost")
                        if commit_fail:
                            # A REAL deferred SQLite constraint fails COMMIT.
                            # This does not simulate a power-loss or fsync I/O failure.
                            c.execute("INSERT INTO commit_guard VALUES (999)")
                    else:
                        guarded_update(c, intent, owner, "completed=?,artifact=?,active_stage=NULL",
                                       (index, output))
                    hit(stage + ".before_commit", crash)
                hit(stage + ".after_commit", crash)
            except LostResponse as exc:
                with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
                    if item["policy"] == "no_broadcast":
                        guarded_update(c, intent, owner,
                                       "state='PENDING',owner=NULL,active_stage=NULL,reason=?", (str(exc),))
                        c.execute("UPDATE pipeline_lock SET owner=NULL WHERE id=1 AND intent=?", (intent,))
                        return "RETRY_BUILD_SAME_DURABLE_INPUT"
                    guarded_update(c, intent, owner,
                                   "state='HALTED',owner=NULL,reason=?", ("unknown RPC semantics: " + str(exc),))
                    return "HALTED"
            except sqlite3.IntegrityError:
                set_halted(root, intent, "raw persistence COMMIT rejected; no emission allowed")
                return "HALTED"

    # An independent read is the only source of bytes for the broadcaster.
    hit("broadcast.before_claim", crash)
    with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
        item = c.execute("SELECT * FROM intents WHERE id=?", (intent,)).fetchone()
        if item["state"] != "READY":
            return item["state"]
        if item["raw"] is None or digest(item["raw"]) != item["raw_hash"]:
            c.execute("UPDATE intents SET state='HALTED', reason='raw bytes missing or changed' WHERE id=?", (intent,))
            return "HALTED"
        c.execute("UPDATE intents SET state='SENDING',owner=? WHERE id=?", (owner, intent))
        persisted_raw = item["raw"]
    hit("broadcast.after_claim", crash)
    hit("broadcast.before_call", crash)
    try:
        receipt = FakeBroadcaster(root, crash, lost).send(persisted_raw)
    except LostResponse as exc:
        with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
            guarded_update(c, intent, owner, "state='READY',owner=NULL,reason=?", (str(exc),))
        return "RETRY_EXACT_BYTES"
    hit("broadcast.after_response", crash)
    with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
        guarded_update(c, intent, owner, "state='ACK',owner=NULL,receipt=?,reason=NULL", (receipt,))
        hit("ack.before_commit", crash)
    hit("ack.after_commit", crash)
    return "ACK"


def recover_after_confirmed_worker_death(root, intent="invoice-1"):
    """Only supervisor AFTER joining dead worker. Not a TTL/lease takeover API."""
    with contextlib.closing(db(Path(root) / "outbox.db")) as c, transaction(c):
        item = c.execute("SELECT * FROM intents WHERE id=?", (intent,)).fetchone()
        if item["state"] == "SENDING":
            # Signed bytes were durable before this state existed.
            assert item["raw"] is not None
            c.execute("UPDATE intents SET state='READY',owner=NULL WHERE id=?", (intent,))
        elif item["state"] == "BUILDING":
            c.execute("UPDATE pipeline_lock SET owner=NULL WHERE id=1 AND intent=?", (intent,))
            if item["active_stage"] and item["policy"] == "unknown":
                c.execute("UPDATE intents SET state='HALTED',owner=NULL,reason='unknown in-flight RPC outcome' WHERE id=?", (intent,))
            else:
                # The exact last committed stage output remains the next input.
                c.execute("UPDATE intents SET state='PENDING',owner=NULL,active_stage=NULL WHERE id=?", (intent,))


def baseline(root, crash=None):
    """INTENTIONALLY WRONG: application receipt is saved only after sending."""
    if row(root)["state"] == "ACK":
        return "ACK"
    wallet = FakeWallet(root)
    data = encode({"intent": "invoice-1", "payload": PAYLOAD})
    for stage in STAGES:
        data = wallet.call(stage, data)
    receipt = FakeBroadcaster(root).send(data)
    hit("baseline.after_send", crash)
    with contextlib.closing(db(Path(root) / "outbox.db")) as c, transaction(c):
        c.execute("UPDATE intents SET state='ACK',receipt=? WHERE id='invoice-1'", (receipt,))
    return "ACK"


def query(root, name, sql, args=()):
    with contextlib.closing(db(Path(root) / name)) as c:
        return [dict(x) for x in c.execute(sql, args)]


def metrics(root):
    return {
        "wallet_calls": query(root, "wallet.db", "SELECT COUNT(*) AS n FROM calls")[0]["n"],
        "broadcast_attempts": query(root, "chain.db", "SELECT COUNT(*) AS n FROM attempts")[0]["n"],
        "distinct_broadcast_bytes": query(root, "chain.db", "SELECT COUNT(DISTINCT byte_id) AS n FROM attempts")[0]["n"],
        "economic_payments": query(root, "chain.db", "SELECT COUNT(*) AS n FROM accepted")[0]["n"],
    }


def child(root, *options, expect=0):
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker", str(root), *options],
                       text=True, capture_output=True, timeout=30)
    assert p.returncode == expect, (options, p.returncode, p.stdout, p.stderr)
    return p


def concurrent_children(root, n=12, *options):
    gate = Path(root) / "go"
    procs = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", str(root),
                              "--gate", str(gate), *options], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True) for _ in range(n)]
    gate.touch()
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, (p.returncode, out, err)
        outputs.append(json.loads(out)["status"])
    return outputs


def one_payment(root):
    m = metrics(root)
    assert m["economic_payments"] == 1, m
    assert m["distinct_broadcast_bytes"] == 1, m
    item = row(root)
    assert item["state"] == "ACK"
    attempts = query(root, "chain.db", "SELECT raw FROM attempts")
    assert all(a["raw"] == item["raw"] for a in attempts)
    return m


def suite():
    results = []
    with tempfile.TemporaryDirectory(prefix="outbox-probe-") as temporary:
        base = Path(temporary)
        def new(name, policy="no_broadcast"):
            root = base / name
            initialize(root)
            submit(root, "invoice-1", PAYLOAD, policy)
            return root
        def record(name, **detail):
            results.append({"test": name, "passed": True, **detail})

        root = new("canonical")
        same = dict(reversed(list(PAYLOAD.items())))
        submit(root, "invoice-1", same)
        for field, value in (("fee_zat", 20_000), ("expiry_height", 210)):
            changed = dict(PAYLOAD, **{field: value})
            try:
                submit(root, "invoice-1", changed)
                raise AssertionError("changed payload accepted")
            except ValueError:
                pass
        changed = json.loads(json.dumps(PAYLOAD))
        changed["recipients"][0]["amount_zat"] += 1
        try:
            submit(root, "invoice-1", changed)
            raise AssertionError("changed amount accepted")
        except ValueError:
            pass
        for bad in (1.0, True, -1):
            invalid = json.loads(json.dumps(PAYLOAD))
            invalid["recipients"][0]["amount_zat"] = bad
            try:
                submit(root, "bad-id", invalid)
                raise AssertionError("noncanonical amount accepted")
            except ValueError:
                pass
        record("canonical intent binding and integer amounts")

        for point in ("intent.before_commit", "intent.after_commit"):
            root = base / point
            initialize(root)
            child(root, "--submit", "--crash", point, expect=CRASH_EXIT)
            assert (row(root) is None) == (point == "intent.before_commit")
            assert metrics(root)["broadcast_attempts"] == 0
            child(root, "--submit")
            one_payment(root)
        record("business intent commit and lost create-ack boundary", cases=2)

        root = new("normal")
        child(root)
        child(root)
        record("ordinary execution and completed retry", **one_payment(root))

        root = new("create-concurrency")
        statuses = concurrent_children(root, 12, "--pause", "0.15", "--submit")
        child(root)
        assert metrics(root)["wallet_calls"] == 4
        record("12 concurrent create/claim callers", statuses=statuses, **one_payment(root))

        root = new("different-intents")
        submit(root, "invoice-2", PAYLOAD)
        first = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", str(root),
                                  "--intent", "invoice-1", "--pause", ".2"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while row(root)["state"] != "BUILDING":
            assert time.monotonic() < deadline, "first builder failed to claim"
            time.sleep(.005)
        second = child(root, "--intent", "invoice-2")
        assert json.loads(second.stdout)["status"] == "BUSY_WALLET"
        out, err = first.communicate(timeout=30)
        assert first.returncode == 0, (out, err)
        child(root, "--intent", "invoice-2")
        calls = query(root, "wallet.db", "SELECT stage,input FROM calls ORDER BY seq")
        assert [x["stage"] for x in calls] == list(STAGES) * 2
        assert metrics(root)["economic_payments"] == 2
        assert row(root, "invoice-2")["state"] == "ACK"
        notes = query(root, "chain.db", "SELECT note FROM accepted ORDER BY note")
        assert [x["note"] for x in notes] == [1, 2]
        record("distinct intents serialize create through extract before input reuse", **metrics(root))

        root = new("held-pipeline")
        submit(root, "invoice-2", PAYLOAD)
        child(root, "--lost", "prove")
        second = child(root, "--intent", "invoice-2")
        assert json.loads(second.stdout)["status"] == "BUSY_WALLET"
        assert metrics(root)["wallet_calls"] == 2
        child(root)
        child(root, "--intent", "invoice-2")
        assert metrics(root)["economic_payments"] == 2
        record("wallet pipeline remains assigned across a pre-broadcast retry")

        root = new("retry-concurrency")
        child(root, "--lost", "broadcast")
        assert row(root)["state"] == "READY"
        concurrent_children(root, 12)
        assert metrics(root)["wallet_calls"] == 4
        assert metrics(root)["broadcast_attempts"] == 2
        record("12 concurrent retries after lost broadcast response", **one_payment(root))

        crash_points = ["before_claim", "after_claim"]
        crash_points += [s + "." + point for s in STAGES for point in
                         ("before_fence", "after_fence", "after_effect", "after_response", "before_commit", "after_commit")]
        crash_points += ["broadcast.before_claim", "broadcast.after_claim", "broadcast.before_call",
                         "broadcast.after_effect", "broadcast.after_response", "ack.before_commit", "ack.after_commit"]
        for i, point in enumerate(crash_points):
            root = new("crash-" + str(i))
            child(root, "--crash", point, expect=CRASH_EXIT)
            before = row(root)
            previous_raw = before["raw"]
            call_count = metrics(root)["wallet_calls"]
            recover_after_confirmed_worker_death(root)
            child(root)
            one_payment(root)
            if previous_raw is not None:
                assert row(root)["raw"] == previous_raw
                assert metrics(root)["wallet_calls"] == call_count
        record("abrupt process death and restart boundary matrix", cases=len(crash_points), points=crash_points)

        safe_details = []
        for stage in STAGES:
            root = new("lost-safe-" + stage)
            child(root, "--lost", stage)
            assert metrics(root)["broadcast_attempts"] == 0
            assert row(root)["state"] == "PENDING"
            child(root)
            calls = query(root, "wallet.db", "SELECT input,output FROM calls WHERE stage=? ORDER BY seq", (stage,))
            assert len(calls) == 2
            assert calls[0]["input"] == calls[1]["input"]
            assert calls[0]["output"] != calls[1]["output"]  # Explicit nondeterminism.
            assert metrics(root)["wallet_calls"] == 5
            safe_details.append({"stage": stage, **one_payment(root)})
        record("known non-broadcasting adapter retries exact durable stage input", cases=safe_details)

        for stage in STAGES:
            root = new("lost-unknown-" + stage, "unknown")
            child(root, "--lost", stage)
            assert row(root)["state"] == "HALTED"
            child(root)
            assert metrics(root)["broadcast_attempts"] == 0
            assert metrics(root)["wallet_calls"] == STAGES.index(stage) + 1
            root = new("crash-unknown-" + stage, "unknown")
            child(root, "--crash", stage + ".after_effect", expect=CRASH_EXIT)
            recover_after_confirmed_worker_death(root)
            child(root)
            assert row(root)["state"] == "HALTED"
            assert metrics(root)["broadcast_attempts"] == 0
        record("unknown RPC contract safely halts instead of blindly rebuilding", cases=8)

        root = new("commit-failure")
        child(root, "--commit-fail")
        assert row(root)["state"] == "HALTED" and row(root)["raw"] is None
        assert metrics(root)["broadcast_attempts"] == 0
        child(root)
        assert metrics(root)["broadcast_attempts"] == 0
        record("actual SQLite deferred-constraint COMMIT rejection cannot emit raw bytes", **metrics(root))

        for condition in ("expiry", "reorg"):
            root = new("risk-" + condition)
            child(root, "--lost", "broadcast")
            raw = row(root)["raw"]
            child(root, *( ["--height", "200"] if condition == "expiry" else ["--reorg"] ))
            assert row(root)["state"] == "HALTED" and row(root)["raw"] == raw
            assert metrics(root)["broadcast_attempts"] == 1
            assert metrics(root)["wallet_calls"] == 4
        root = new("reorg-after-ack")
        child(root)
        child(root, "--reorg")
        assert row(root)["state"] == "HALTED"
        assert metrics(root)["broadcast_attempts"] == 1
        record("expiry/reorg ambiguity stops automatic replacement", cases=3)

        root = new("tamper")
        child(root, "--crash", "extract.after_commit", expect=CRASH_EXIT)
        with contextlib.closing(db(root / "outbox.db")) as c, transaction(c):
            c.execute("UPDATE intents SET raw=? WHERE id='invoice-1'", (b"changed",))
        child(root)
        assert row(root)["state"] == "HALTED" and metrics(root)["broadcast_attempts"] == 0
        record("stored raw byte-integrity mismatch fails closed")

        root = new("live-owner")
        child(root, "--crash", "create.after_fence", expect=CRASH_EXIT)
        # Intentionally omit supervisor recovery: ordinary retries must not steal.
        child(root)
        assert row(root)["state"] == "BUILDING" and metrics(root)["wallet_calls"] == 0
        record("ordinary retry never steals an unresolved build claim")

        root = new("baseline")
        child(root, "--baseline", "--crash", "baseline.after_send", expect=CRASH_EXIT)
        assert metrics(root)["economic_payments"] == 1 and row(root)["state"] != "ACK"
        child(root, "--baseline")
        m = metrics(root)
        accepted = query(root, "chain.db", "SELECT note,amount_zat FROM accepted ORDER BY note")
        assert m["economic_payments"] == 2 and m["distinct_broadcast_bytes"] == 2
        assert accepted[0]["note"] != accepted[1]["note"]
        record("adverse send-first/save-later baseline duplicates payment", unrelated_notes=[x["note"] for x in accepted], **m)

    return {
        "probe": "local fake-wallet durable payment outbox",
        "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
        "passed_groups": len(results), "durability_boundary_cases": 35, "failures": 0,
        "scope": "process crashes and logical SQLite commit failure; no real Zcash integration",
        "results": results,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("test")
    s.add_argument("--json", type=Path)
    w = sub.add_parser("worker")
    w.add_argument("root", type=Path)
    w.add_argument("--intent", default="invoice-1")
    w.add_argument("--crash")
    w.add_argument("--lost", choices=(*STAGES, "broadcast"))
    w.add_argument("--commit-fail", action="store_true")
    w.add_argument("--height", type=int, default=100)
    w.add_argument("--reorg", action="store_true")
    w.add_argument("--baseline", action="store_true")
    w.add_argument("--submit", action="store_true")
    w.add_argument("--pause", type=float, default=0)
    w.add_argument("--gate", type=Path)
    args = p.parse_args()
    if args.command == "test":
        result = suite()
        if args.json:
            args.json.write_text(json.dumps(result, indent=2) + "\n")
        print(f"PASS: {result['passed_groups']} groups; {result['durability_boundary_cases']} actual process-crash durability boundaries; 0 failures")
        print("Safe model: one fixture payment; retries broadcast only committed identical bytes.")
        print("Adverse baseline: two fixture payments from unrelated notes after send/save crash.")
        print("Limit: fake wallet/chain only; no global exactly-once, real RPC, consensus, or power-loss proof.")
    else:
        if args.gate:
            deadline = time.monotonic() + 20
            while not args.gate.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test start gate")
                time.sleep(.005)
        if args.submit:
            submit(args.root, args.intent, PAYLOAD, crash=args.crash)
        status = (baseline(args.root, args.crash) if args.baseline else
                  drive(args.root, intent=args.intent, crash=args.crash, lost=args.lost,
                        commit_fail=args.commit_fail, height=args.height,
                        reorg=args.reorg, pause=args.pause))
        print(json.dumps({"status": status}))


if __name__ == "__main__":
    main()
