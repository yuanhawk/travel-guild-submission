"""Dashboard persistence store — Phase 1 local SQLite (swap-seam for PolarDB/Tair).

Backs the user dashboard's stateful side-effects (the consent-split held plans,
booked trips, demo users, saved items). It is STRICTLY OFF the deterministic var-0
path: nothing here is read inside `negotiate()` / `_request_digest`; rows are written
AFTER a result is final and read only by the HTTP layer. Mirrors the proven
`mcp_services/travel_memory/store.py` SqliteStore shape (write lock, check_same_thread=False,
Row factory, _migrate via executescript, get_store()/set_store() singleton).

Money convention: ALL price columns are INTEGER CENTS (never float). JSON blobs are
serialized with sort_keys=True so they are byte-stable.

KEY DESIGN (from the consolidated build plan): the consent-split "plan", the booked
"trip" and the "booking" are ONE row in `trips`, keyed by the deterministic
`idempotency_key` (= "trip-<request_digest>"), differing only by lifecycle `status`
(plan_ready → booked → cancelled). The volatile merchant `checkout_id` is a
SERVER-ONLY indexed column — never the PK, never echoed to the client.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any


class DashboardStore:
    """Abstract interface — Phase 2 swaps in a PolarDB/Tair-backed implementation
    with the identical surface. All money in/out is INTEGER CENTS."""

    # demo users (pre-seeded; NO register endpoint)
    def list_demo_users(self) -> list[dict]: raise NotImplementedError
    def get_user(self, user_id: str) -> dict | None: raise NotImplementedError
    def upsert_demo_user(self, row: dict) -> None: raise NotImplementedError
    # trips (unified plan_ready → booked → cancelled lifecycle)
    def save_plan(self, row: dict) -> None: raise NotImplementedError
    def update_envelope(self, idempotency_key: str, envelope: dict, *, now: str = "") -> None: raise NotImplementedError
    def get_plan(self, idempotency_key: str) -> dict | None: raise NotImplementedError
    # L10 — any row sharing the given content DIGEST that is currently BOOKED
    # (see get_booked_row_by_digest's docstring on the concrete implementation
    # for the derived-key wallet-sharing hazard this closes).
    def get_booked_row_by_digest(self, digest: str) -> dict | None: raise NotImplementedError
    def mark_booked(self, idempotency_key: str, *, booking_ref: str, envelope: dict,
                    confirmed_at: str) -> bool: raise NotImplementedError
    def mark_cancelled(self, idempotency_key: str, *, refunded_cents: int) -> None: raise NotImplementedError
    def mark_superseded(self, idempotency_key: str, *, child_idempotency_key: str) -> None: raise NotImplementedError
    def clear_superseded(self, idempotency_key: str) -> None: raise NotImplementedError
    def list_trips(self, user_id: str) -> list[dict]: raise NotImplementedError
    def get_trip(self, idempotency_key: str) -> dict | None: raise NotImplementedError
    def sweep_expired(self, *, older_than_iso: str) -> int: raise NotImplementedError
    # M7 — lightweight idempotency record for ATOMIC (commit=True) negotiate()
    # successes, which are never persisted as a full trips-store row (only
    # plan_ready/plan-mode results are, via save_plan). See orchestrator.py's
    # `_atomic_commit_marker`/`_atomic_committed_lookup` docstrings.
    def mark_atomic_committed(self, idempotency_key: str, *, booking_ref: str = "",
                              now: str = "") -> None: raise NotImplementedError
    def was_atomic_committed(self, idempotency_key: str) -> bool: raise NotImplementedError
    # notifications (send-once markers for STATIC one-time pushes, e.g. #195 planning_note)
    def mark_notification_sent(self, idempotency_key: str, kind: str, *, now: str = "") -> None:
        raise NotImplementedError
    def was_notification_sent(self, idempotency_key: str, kind: str) -> bool:
        raise NotImplementedError
    # saved / favourites
    def add_saved(self, row: dict) -> int: raise NotImplementedError
    def list_saved(self, user_id: str) -> list[dict]: raise NotImplementedError
    def remove_saved(self, user_id: str, saved_id: int) -> None: raise NotImplementedError
    # conversations (B2: session-context layer — threaded refine turns)
    def create_conversation(self, session_id: str, user_id: str, active_idempotency_key: str,
                            *, seed_turns: list | None = None, now: str | None = None) -> None:
        raise NotImplementedError
    def get_conversation(self, session_id: str) -> dict | None: raise NotImplementedError
    def get_conversation_by_active_key(self, idempotency_key: str) -> dict | None: raise NotImplementedError
    def append_turns(self, session_id: str, *, turns: list[dict],
                     new_active_key: str | None = None, now: str | None = None) -> None:
        raise NotImplementedError


class SqliteDashboardStore(DashboardStore):
    """SQLite-backed store. db_path=":memory:" for tests; a file for persistence."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()  # RLock: sweep_expired re-enters from save_plan
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # H3 fix — see set_active_key_guard()'s docstring. None (default) =>
        # no exclusion, byte-identical to before this fix for any caller that
        # never wires it (direct store use in a script/test).
        self._active_key_guard: "Any" = None
        self._migrate()
        self._start_background_sweep()

    def set_active_key_guard(self, guard: "Any") -> None:
        """H3 fix — inject a zero-arg callable returning the set of
        idempotency_keys CURRENTLY held by an in-flight /confirm, /refine, or
        /replan (server.py wires this to `lambda: set(_state.trip_locks)` at
        app startup — the same per-idk lock registry _get_trip_lock() already
        uses to serialize those handlers against EACH OTHER).

        Both sweep_expired() call sites (the 10-minute background thread AND
        save_plan()'s MAX_TRIPS overflow sweep) consult this before deleting a
        plan_ready row, so neither sweep can delete a row while it is mid-
        commit — the exact race that could otherwise leave a completed
        merchant debit with no corresponding row anywhere in the store (a
        /confirm's mark_booked write racing the sweep's delete of the SAME
        row it just read as plan_ready).

        `guard` may be None to remove the exclusion (byte-identical to before
        this fix)."""
        self._active_key_guard = guard

    def _locked_keys(self) -> set:
        if self._active_key_guard is None:
            return set()
        try:
            return set(self._active_key_guard())
        except Exception:  # noqa: BLE001 — the guard is a safety net, never a crash source
            return set()

    def _start_background_sweep(self) -> None:
        """Launch a daemon thread that calls sweep_expired every 10 minutes."""
        def _sweep_loop(store: SqliteDashboardStore, interval: int = 600) -> None:
            import time
            import datetime
            while True:
                time.sleep(interval)
                try:
                    cutoff = (datetime.datetime.now(datetime.timezone.utc)
                              - datetime.timedelta(hours=24)).isoformat()
                    store.sweep_expired(older_than_iso=cutoff)
                except Exception:
                    pass

        t = threading.Thread(target=_sweep_loop, args=(self,), daemon=True)
        t.start()

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS demo_users (
                user_id              TEXT PRIMARY KEY,
                display_name         TEXT NOT NULL,
                persona              TEXT,
                nationality          TEXT,
                home_currency        TEXT DEFAULT 'USD',
                wallet_balance_cents INTEGER DEFAULT 0,
                prefs_json           TEXT,
                created_at           TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS trips (
                idempotency_key      TEXT PRIMARY KEY,   -- 'trip-<digest>' = client confirm/cancel handle
                user_id              TEXT NOT NULL,
                digest               TEXT,
                checkout_id          TEXT,               -- merchant 'co-local-N' SERVER-ONLY, never echoed
                dest_token           TEXT,
                status               TEXT NOT NULL CHECK (status IN ('plan_ready','booked','cancelled')),
                outcome              TEXT,
                booking_ref          TEXT,
                package_total_cents  INTEGER,
                refunded_cents       INTEGER NOT NULL DEFAULT 0,
                request_json         TEXT,
                envelope_json        TEXT NOT NULL,
                created_at           TEXT NOT NULL,
                confirmed_at         TEXT,
                updated_at           TEXT NOT NULL,
                owner_token          TEXT NOT NULL DEFAULT '',  -- anon-trip ownership secret (IDOR fix, tier 2)
                merchant_user_id     TEXT NOT NULL DEFAULT '',  -- #161 canonical Go-merchant checkout owner (see utils/ucp_signing.merchant_checkout_owner)
                superseded_by        TEXT NOT NULL DEFAULT ''  -- cross-generation double-book fix: set to the child idempotency_key when a successful /refine replaces this row; a superseded row can never be /confirm'd (see confirm()'s superseded_by guard)
            );
            CREATE INDEX IF NOT EXISTS idx_trips_user     ON trips(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trips_checkout ON trips(checkout_id);
            CREATE TABLE IF NOT EXISTS saved_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                item_type   TEXT NOT NULL,
                item_ref    TEXT NOT NULL,
                payload_json TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, item_type, item_ref)
            );
            CREATE TABLE IF NOT EXISTS conversations (
                session_id              TEXT PRIMARY KEY,
                user_id                 TEXT NOT NULL DEFAULT '',
                active_idempotency_key  TEXT NOT NULL,
                turns_json              TEXT NOT NULL DEFAULT '[]',
                created_at              TEXT NOT NULL,
                updated_at              TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_active ON conversations(active_idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_conv_user   ON conversations(user_id, updated_at DESC);
            -- #195 send-once markers: one row per (trip, notification kind). A STATIC
            -- one-time push (e.g. 'planning_note') records a marker here so it never
            -- re-fires on a periodic aftercare poll. New table (CREATE IF NOT EXISTS) —
            -- no ALTER needed on an existing prod DB. Never on the var-0 path.
            CREATE TABLE IF NOT EXISTS sent_notifications (
                idempotency_key TEXT NOT NULL,
                kind            TEXT NOT NULL,
                sent_at         TEXT NOT NULL,
                PRIMARY KEY (idempotency_key, kind)
            );
            -- M7 fix — lightweight idempotency record for ATOMIC (commit=True,
            -- i.e. no `plan` in the request) negotiate() successes. These are
            -- NEVER persisted as a full `trips` row (only plan_ready/plan-mode
            -- results are, via save_plan) — so a retried identical atomic
            -- POST /negotiate had no record anywhere for _fund_wallet's guard
            -- to find, and would re-fund+re-debit the wallet for a second live
            -- merchant booking. One row per committed atomic idempotency_key;
            -- no envelope/request stored (deliberately minimal — this is an
            -- idempotency marker, not a trip record).
            CREATE TABLE IF NOT EXISTS atomic_bookings (
                idempotency_key TEXT PRIMARY KEY,
                booking_ref     TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL
            );
        """)
        self._conn.commit()
        # Idempotent migration for DB files created before owner_token existed
        # (IDOR fix, tier 2 — anon-trip ownership). CREATE TABLE above only
        # applies to a brand-new file; existing files need ALTER TABLE.
        try:
            self._conn.execute(
                "ALTER TABLE trips ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        except Exception:
            pass  # column already present
        # #161 — idempotent migration for DB files created before merchant_user_id
        # existed (canonical Go-merchant checkout owner; write-once, mirrors owner_token).
        try:
            self._conn.execute(
                "ALTER TABLE trips ADD COLUMN merchant_user_id TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        except Exception:
            pass  # column already present
        # Cross-generation double-book fix — idempotent migration for DB files
        # created before superseded_by existed (mirrors owner_token/
        # merchant_user_id above).
        try:
            self._conn.execute(
                "ALTER TABLE trips ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        except Exception:
            pass  # column already present

    # ---- demo users -------------------------------------------------------
    def list_demo_users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, display_name, persona, nationality, home_currency, "
                "wallet_balance_cents FROM demo_users ORDER BY user_id").fetchall()
        return [dict(r) for r in rows]

    def get_user(self, user_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM demo_users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["prefs"] = json.loads(d.pop("prefs_json") or "{}")
        return d

    def upsert_demo_user(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO demo_users
                   (user_id, display_name, persona, nationality, home_currency,
                    wallet_balance_cents, prefs_json)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     display_name=excluded.display_name, persona=excluded.persona,
                     nationality=excluded.nationality, home_currency=excluded.home_currency,
                     wallet_balance_cents=excluded.wallet_balance_cents,
                     prefs_json=excluded.prefs_json""",
                (row["user_id"], row.get("display_name", row["user_id"]),
                 row.get("persona"), row.get("nationality"),
                 row.get("home_currency", "USD"), int(row.get("wallet_balance_cents", 0)),
                 json.dumps(row.get("prefs") or {}, sort_keys=True)))
            self._conn.commit()

    # ---- trips (consent-split lifecycle) ----------------------------------
    def save_plan(self, row: dict) -> None:
        """Persist a plan_ready trip (idempotent on idempotency_key — a re-POST of
        the same plan overwrites the held envelope, never duplicates)."""
        now = row.get("now") or _now()
        _MAX_TRIPS = int(os.environ.get("MAX_TRIPS_PER_USER", "500"))
        with self._lock:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM trips WHERE user_id=?",
                (row["user_id"],)).fetchone()[0]
            if count >= _MAX_TRIPS:
                self.sweep_expired(  # RLock allows re-entry from this same thread
                    older_than_iso=_now())
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM trips WHERE user_id=?",
                    (row["user_id"],)).fetchone()[0]
                if count >= _MAX_TRIPS:
                    raise RuntimeError(
                        f"trip_store_full: max {_MAX_TRIPS} trips per user")
            self._conn.execute(
                """INSERT INTO trips
                   (idempotency_key, user_id, digest, checkout_id, dest_token, status,
                    outcome, booking_ref, package_total_cents, request_json,
                    envelope_json, created_at, updated_at, owner_token, merchant_user_id)
                   VALUES (?,?,?,?,?, 'plan_ready', 'plan_ready', NULL, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(idempotency_key) DO UPDATE SET
                     checkout_id=excluded.checkout_id, dest_token=excluded.dest_token,
                     package_total_cents=excluded.package_total_cents,
                     request_json=excluded.request_json, envelope_json=excluded.envelope_json,
                     updated_at=excluded.updated_at,
                     owner_token=COALESCE(NULLIF(trips.owner_token,''), excluded.owner_token),
                     merchant_user_id=COALESCE(NULLIF(trips.merchant_user_id,''), excluded.merchant_user_id)
                   WHERE trips.status='plan_ready'""",  # never clobber a booked/cancelled row
                (row["idempotency_key"], row["user_id"], row.get("digest"),
                 row.get("checkout_id"), row.get("dest_token"),
                 int(row.get("package_total_cents") or 0),
                 json.dumps(row.get("request") or {}, sort_keys=True),
                 json.dumps(row["envelope"], sort_keys=True), now, now,
                 (row.get("owner_token") or "").strip(),
                 (row.get("merchant_user_id") or "").strip()))
            self._conn.commit()

    def update_envelope(self, idempotency_key: str, envelope: dict, *, now: str = "") -> None:
        """Update ONLY the envelope_json of an existing plan_ready OR booked row.
        Preserves status, money columns (package_total_cents/checkout_id/dest_token),
        and booking_ref — used by /replan to apply item-level POI edits to a BOOKED
        trip (the priced package is never touched). Cancelled rows are never updated
        (WHERE status guard); a no-op on a missing/cancelled row."""
        ts = now or _now()
        with self._lock:
            self._conn.execute(
                """UPDATE trips SET envelope_json=?, updated_at=?
                   WHERE idempotency_key=? AND status IN ('plan_ready','booked')""",
                (json.dumps(envelope, sort_keys=True), ts, idempotency_key))
            self._conn.commit()

    def get_plan(self, idempotency_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM trips WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["envelope"] = json.loads(d["envelope_json"])
        d["request"] = json.loads(d["request_json"] or "{}")
        return d

    get_trip = get_plan  # same row, alias for the /trips/{key} read

    def get_booked_row_by_digest(self, digest: str) -> dict | None:
        """L10 fix — a derived-key row (idk-vN, minted by the M1/M4/M5/M6
        consent-plan-mismatch fork guard) shares its content `digest` column
        with whatever OTHER row(s) were persisted for the SAME request content
        — including the original row it forked away from. Crucially, both
        rows' underlying merchant/wallet session is ALSO keyed by that SAME
        digest (orchestrator._wallet_session_id = idempotency_key = the
        digest-based key computed at negotiate() time, BEFORE server.py's
        fork-time key derivation ever runs) — so the wallet a derived row's
        booking actually debited is the DIGEST-keyed wallet, not the row's own
        (derived) store key.

        M8's wallet-fund-reset guard (`_fund_wallet`'s `trip_lookup` check)
        only looks up the row stored under the EXACT key it is about to fund
        (the digest key) — it has no way to see that a DIFFERENT row (a
        derived key sharing this digest) is currently booked. This method
        closes that gap: given the content digest, return any row (regardless
        of its own idempotency_key) that is currently `status='booked'`, so
        `_fund_wallet` can extend its guard to cover the shared-wallet case."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM trips WHERE digest=? AND status='booked' LIMIT 1",
                (digest,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["envelope"] = json.loads(d["envelope_json"])
        d["request"] = json.loads(d["request_json"] or "{}")
        return d

    def mark_booked(self, idempotency_key: str, *, booking_ref: str, envelope: dict,
                    confirmed_at: str) -> bool:
        # HIGH — TOCTOU defense-in-depth (cross-generation double-book fix):
        # in addition to status='plan_ready', require superseded_by='' in the
        # SAME atomic UPDATE. Without this, a /confirm that read a stale
        # (pre-stamp) row could still book a row that a concurrent /refine has
        # SINCE stamped superseded — status stays 'plan_ready' until a booking
        # actually lands, so the old WHERE (status only) could not detect that.
        # server.py's per-idk lock (_get_trip_lock) is the primary fix that
        # prevents the interleaving outright; this compare-and-set is a second,
        # independent guard against any caller that reaches mark_booked without
        # holding that lock.
        #
        # H3 fix — returns whether the UPDATE genuinely matched a row. The
        # per-idk trip lock + the sweep exclusion (set_active_key_guard) are
        # the PRIMARY fixes that prevent a sweep from ever deleting a row
        # while /confirm is mid-commit; this rowcount check is defense-in-
        # depth so that IF the row is somehow gone by the time this write
        # runs (e.g. an operator-triggered manual sweep, or a store bug), the
        # caller can detect and honestly surface it instead of silently
        # believing the write succeeded.
        with self._lock:
            cur = self._conn.execute(
                """UPDATE trips SET status='booked', outcome='success',
                   booking_ref=?, envelope_json=?, confirmed_at=?, updated_at=?
                   WHERE idempotency_key=? AND status='plan_ready' AND superseded_by=''""",
                (booking_ref, json.dumps(envelope, sort_keys=True), confirmed_at,
                 confirmed_at, idempotency_key))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_cancelled(self, idempotency_key: str, *, refunded_cents: int) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """UPDATE trips SET status='cancelled', refunded_cents=?, updated_at=?
                   WHERE idempotency_key=? AND status='booked'""",
                (int(refunded_cents), now, idempotency_key))
            self._conn.commit()

    def mark_superseded(self, idempotency_key: str, *, child_idempotency_key: str) -> None:
        """Cross-generation double-book fix: stamp a still-open plan_ready row as
        superseded by the NEW row a successful /refine just created. Only ever
        applied to a plan_ready row (booked/cancelled are terminal and refine()
        already refuses to refine them) — the WHERE guard makes this a safe
        no-op if called twice or on the wrong status. confirm() refuses to book
        any row with a non-empty superseded_by (see server.py)."""
        now = _now()
        with self._lock:
            self._conn.execute(
                """UPDATE trips SET superseded_by=?, updated_at=?
                   WHERE idempotency_key=? AND status='plan_ready'""",
                (child_idempotency_key, now, idempotency_key))
            self._conn.commit()

    def clear_superseded(self, idempotency_key: str) -> None:
        """H2 fix — reset a plan_ready row's `superseded_by` back to '' when a
        /refine's re-plan lands on this EXISTING key again (a revert to an
        earlier generation's exact parameters re-mints that generation's
        deterministic idempotency_key — see server.py's refine() step 8). This
        row is reclaiming the "current generation" slot, so any STALE pointer
        left over from when it was originally superseded-away must be cleared
        BEFORE the caller marks the row being refined FROM as superseded by
        this one — otherwise both rows would point at each other (a mutual
        superseded_by cycle) and neither could ever be /confirm'd. Only ever
        applied to a plan_ready row (booked/cancelled are terminal); a safe
        no-op otherwise (mirrors mark_superseded's WHERE guard)."""
        now = _now()
        with self._lock:
            self._conn.execute(
                """UPDATE trips SET superseded_by='', updated_at=?
                   WHERE idempotency_key=? AND status='plan_ready'""",
                (now, idempotency_key))
            self._conn.commit()

    def list_trips(self, user_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idempotency_key, user_id, status, outcome, booking_ref, "
                "package_total_cents, created_at, confirmed_at FROM trips "
                "WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def sweep_expired(self, *, older_than_iso: str) -> int:
        """Delete abandoned never-confirmed plans older than a cutoff. Returns count.

        H3 fix — excludes any idempotency_key CURRENTLY held by an in-flight
        /confirm, /refine, or /replan (see set_active_key_guard). Without
        this, this sweep (called both from the 10-minute background thread
        AND from save_plan()'s MAX_TRIPS overflow guard with a cutoff of
        "now" — i.e. every un-expired plan_ready row, across EVERY user, not
        just the one hitting the cap) could delete a row between /confirm's
        own read and its later mark_booked write, leaving a completed
        merchant debit with no corresponding row anywhere in the store."""
        locked = self._locked_keys()
        with self._lock:
            if locked:
                placeholders = ",".join("?" for _ in locked)
                cur = self._conn.execute(
                    f"DELETE FROM trips WHERE status='plan_ready' AND created_at < ? "
                    f"AND idempotency_key NOT IN ({placeholders})",
                    (older_than_iso, *locked))
            else:
                cur = self._conn.execute(
                    "DELETE FROM trips WHERE status='plan_ready' AND created_at < ?",
                    (older_than_iso,))
            self._conn.commit()
            return cur.rowcount

    def mark_atomic_committed(self, idempotency_key: str, *, booking_ref: str = "",
                              now: str = "") -> None:
        """M7 fix — record that an ATOMIC (commit=true, no `plan` in the
        request) negotiate() genuinely committed a merchant booking under this
        idempotency_key. Idempotent upsert (never errors on a repeat call for
        the same key — mirrors the other mark_* methods' safe-no-op-on-repeat
        contract); the booking_ref is kept from the FIRST call only (a repeat
        call — e.g. a defensive double-invocation — must never silently
        overwrite the true original booking_ref with something else)."""
        ts = now or _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO atomic_bookings (idempotency_key, booking_ref, created_at)
                   VALUES (?,?,?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (idempotency_key, booking_ref or "", ts))
            self._conn.commit()

    def was_atomic_committed(self, idempotency_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM atomic_bookings WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
        return row is not None

    # ---- notifications (send-once markers) --------------------------------
    def mark_notification_sent(self, idempotency_key: str, kind: str, *, now: str = "") -> None:
        """Record that a STATIC one-time notification of `kind` was sent for this trip.
        Idempotent (INSERT OR IGNORE — a re-mark never errors or overwrites sent_at)."""
        ts = now or _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO sent_notifications (idempotency_key, kind, sent_at)
                   VALUES (?, ?, ?)""",
                (idempotency_key, kind, ts))
            self._conn.commit()

    def was_notification_sent(self, idempotency_key: str, kind: str) -> bool:
        """True iff a `kind` notification was already marked sent for this trip."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sent_notifications WHERE idempotency_key=? AND kind=?",
                (idempotency_key, kind)).fetchone()
        return row is not None

    # ---- saved / favourites ----------------------------------------------
    def add_saved(self, row: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO saved_items (user_id, item_type, item_ref, payload_json)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user_id, item_type, item_ref) DO UPDATE SET
                     payload_json=excluded.payload_json""",
                (row["user_id"], row["item_type"], row["item_ref"],
                 json.dumps(row.get("payload") or {}, sort_keys=True)))
            self._conn.commit()
            return cur.lastrowid or 0

    def list_saved(self, user_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saved_items WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def remove_saved(self, user_id: str, saved_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM saved_items WHERE user_id=? AND id=?", (user_id, saved_id))
            self._conn.commit()

    # ---- conversations (B2: session-context layer) --------------------------

    def create_conversation(self, session_id: str, user_id: str, active_idempotency_key: str,
                            *, seed_turns: list | None = None, now: str | None = None) -> None:
        """Create a new conversation thread (INSERT OR IGNORE — idempotent)."""
        ts = now or _now()
        turns = json.dumps(seed_turns or [], sort_keys=True)
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO conversations
                   (session_id, user_id, active_idempotency_key, turns_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, user_id or "", active_idempotency_key, turns, ts, ts),
            )
            self._conn.commit()

    def get_conversation(self, session_id: str) -> dict | None:
        """Fetch a conversation by session_id; parses turns_json. Returns None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["turns"] = json.loads(d.pop("turns_json") or "[]")
        return d

    def get_conversation_by_active_key(self, idempotency_key: str) -> dict | None:
        """Find the most recent conversation that points at this idempotency_key."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE active_idempotency_key=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["turns"] = json.loads(d.pop("turns_json") or "[]")
        return d

    def append_turns(self, session_id: str, *, turns: list[dict],
                     new_active_key: str | None = None, now: str | None = None) -> None:
        """Append turns to the conversation; optionally repoint active_idempotency_key."""
        ts = now or _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT turns_json, active_idempotency_key FROM conversations WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                return  # conversation does not exist — caller must create first
            existing = json.loads(row["turns_json"] or "[]")
            merged = existing + list(turns)
            new_key = new_active_key if new_active_key is not None else row["active_idempotency_key"]
            self._conn.execute(
                "UPDATE conversations SET turns_json=?, active_idempotency_key=?, updated_at=? "
                "WHERE session_id=?",
                (json.dumps(merged, sort_keys=True), new_key, ts, session_id),
            )
            self._conn.commit()


def _now() -> str:
    """ISO-8601 UTC timestamp (store metadata only — NEVER part of any var-0 output)."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- module singleton (swap seam) -----------------------------------------
_default_store: DashboardStore | None = None
_store_lock = threading.Lock()


def get_store() -> DashboardStore:
    """The process store. Defaults to SQLite at $TRAVEL_DASHBOARD_DB (or :memory:)."""
    global _default_store
    if _default_store is None:
        with _store_lock:
            if _default_store is None:
                _default_store = SqliteDashboardStore(
                    os.environ.get("TRAVEL_DASHBOARD_DB", ":memory:"))
    return _default_store


def set_store(store: DashboardStore) -> None:
    """Override the singleton (test injection / Phase-2 swap)."""
    global _default_store
    _default_store = store
