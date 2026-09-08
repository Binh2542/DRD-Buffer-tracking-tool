import hashlib
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

DEVICES_COLLECTION = "devices"
COMPLETED_COLLECTION = "completed_devices"
ACCOUNTS_COLLECTION = "app_accounts"
# Department scan-page (public/*/index.html) logins - separate from
# ACCOUNTS_COLLECTION (the desktop app's own accounts), see set_queue_account.
QUEUE_ACCOUNTS_COLLECTION = "queue_accounts"
ONLINE_REPAIR_COLLECTION = "online_repairs"

# Assembly Rework mode's own collections, kept entirely separate from the
# Debug mode ones above so scans in one mode never mix with the other.
AR_DEVICES_COLLECTION = "ar_devices"
AR_COMPLETED_COLLECTION = "ar_completed_devices"
AR_ONLINE_REPAIR_COLLECTION = "ar_online_repairs"

# MRB ("scrap") - a terminal state for a device like Completed, but never
# reached via Completed's own path, and explicitly never written there.
MRB_COLLECTION = "mrb_devices"
AR_MRB_COLLECTION = "ar_mrb_devices"

# Paper-QR-reassignment tracking - deliberately a single shared collection,
# not split debug_*/ar_* like everything else above, since the underlying
# PCB (identified by its permanent pcb_qr, never the reassignable paper
# QR) is the same physical thing regardless of which app mode happened to
# be active when someone scanned it.
QR_CHANGES_COLLECTION = "qr_changes"

# WebDB board lookups that ultimately came back empty after every retry
# (see webdb_client._lookup_board) - logged here so failures on someone
# else's machine are visible centrally instead of only in a local console
# nobody but that operator ever sees.
LOOKUP_FAILURES_COLLECTION = "lookup_failures"
# A PROD5 Google Sheet write that failed even after prod5_sheet.py's own
# retries - see log_sheet_write_failure below.
SHEET_WRITE_FAILURES_COLLECTION = "sheet_write_failures"

# Department QR-scan queue, written by the public web pages under public/
# (see firestore.rules) - one doc per {department}__{qr}. The desktop app
# never writes to this collection, only removes an entry once Online
# Repair picks that unit back up (see remove_io_queue_entries below).
IO_QUEUE_COLLECTION = "io_queue"

_PASSWORD_SALT = "drd-buffer-tracking-tool"


def init_firestore(key_path: Path):
    """Initialize (once per process) and return a Firestore client."""
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(key_path))
        firebase_admin.initialize_app(cred)
    return firestore.client()


def add_device(db, device: dict, collection: str = DEVICES_COLLECTION) -> None:
    db.collection(collection).document(device["qr"]).set(device)


def _delete_from(db, collection: str, qrs) -> None:
    batch = db.batch()
    for qr in qrs:
        batch.delete(db.collection(collection).document(qr))
    batch.commit()


def delete_devices(db, qrs, collection: str = DEVICES_COLLECTION) -> None:
    _delete_from(db, collection, qrs)


def delete_completed_devices(db, qrs, collection: str = COMPLETED_COLLECTION) -> None:
    _delete_from(db, collection, qrs)


def list_devices(db, collection: str = DEVICES_COLLECTION) -> list[dict]:
    return [doc.to_dict() for doc in db.collection(collection).stream()]


def listen_devices(db, on_change, collection: str = DEVICES_COLLECTION):
    """Subscribe to live changes on a devices-shaped collection (Debug
    mode's `devices` by default, or Assembly Rework's `ar_devices`).

    `on_change` is called with the full current list of devices, both once
    immediately (initial snapshot) and again on every add/update/delete from
    any connected client. Returns a watch handle - call .unsubscribe() on it
    to stop listening (e.g. on logout/close).
    """

    def _callback(col_snapshot, _changes, _read_time):
        on_change([doc.to_dict() for doc in col_snapshot])

    return db.collection(collection).on_snapshot(_callback)


def add_completed_device(db, device: dict, collection: str = COMPLETED_COLLECTION) -> None:
    """Record a device that passed Prog Main/Test and was moved out of the buffer."""
    db.collection(collection).document(device["qr"]).set(device)


def fetch_completed_since(db, since_iso: str, collection: str = COMPLETED_COLLECTION) -> list[dict]:
    """One-time (non-live) fetch of completed devices back to since_iso -
    for Chart/Summary, which previously called list_devices (a full,
    unfiltered read of the ENTIRE collection) and then filtered by date
    client-side in Python. The date-range picker in both views made that
    filtering look server-side, but every click - even picking "7 days" -
    still paid for reading the whole collection's history from scratch;
    with this collection only ever growing, that cost grows with it
    forever. Filtering with a Firestore query instead (same pattern
    fetch_online_repairs_since already uses) means the read cost tracks
    the selected range, not the collection's total size."""
    query = db.collection(collection).where(filter=FieldFilter("complete_time_iso", ">=", since_iso))
    return [doc.to_dict() for doc in query.stream()]


def listen_completed_devices(db, on_change, collection: str = COMPLETED_COLLECTION):
    """Same as listen_devices, but for a completed-devices-shaped collection."""

    def _callback(col_snapshot, _changes, _read_time):
        on_change([doc.to_dict() for doc in col_snapshot])

    return db.collection(collection).on_snapshot(_callback)


def add_online_repair(db, record: dict, collection: str = ONLINE_REPAIR_COLLECTION) -> None:
    """Log one "Online Repair" success event. Unlike devices/completed
    (keyed by qr, one live document per device), the same QR can
    legitimately show up here multiple times over time (repaired again
    later) - so this is an append-only log with an auto-generated doc id,
    not a keyed-by-qr current-state document."""
    db.collection(collection).document().set(record)


def delete_online_repairs(db, doc_ids, collection: str = ONLINE_REPAIR_COLLECTION) -> None:
    _delete_from(db, collection, doc_ids)


def listen_online_repairs(db, on_change, collection: str = ONLINE_REPAIR_COLLECTION, since_iso: str | None = None):
    """Same as listen_devices, but for an online-repairs-shaped log. Since
    these documents have auto-generated ids (not qr-keyed), each record has
    the Firestore document id injected as "_doc_id" so the UI has a stable,
    unique key to select/delete by even when the same qr repeats.

    `since_iso`, when given, bounds the live listener to only documents
    whose repair_time_iso is on or after this ISO-8601 timestamp (see
    app_gui.py's rolling Online Repair window). Online Repair is used every
    day, so unlike Buffer this collection would otherwise grow forever
    while still paying a full-collection read on every single write to
    every connected listener - bounding it keeps that fan-out cost flat
    regardless of how much history has piled up. Pass None (the default)
    for the old unbounded behavior."""

    def _callback(col_snapshot, _changes, _read_time):
        records = []
        for doc in col_snapshot:
            record = doc.to_dict()
            record["_doc_id"] = doc.id
            records.append(record)
        on_change(records)

    query = db.collection(collection)
    if since_iso is not None:
        query = query.where(filter=FieldFilter("repair_time_iso", ">=", since_iso))
    return query.on_snapshot(_callback)


def fetch_online_repairs_since(db, since_iso: str, collection: str = ONLINE_REPAIR_COLLECTION) -> list[dict]:
    """One-time (non-live) fetch of repair records back to since_iso - for
    Chart/Summary when the user picks a look-back range wider than
    listen_online_repairs' live window, instead of ever holding more than
    that window of this collection in a permanent listener."""
    query = db.collection(collection).where(filter=FieldFilter("repair_time_iso", ">=", since_iso))
    records = []
    for doc in query.stream():
        record = doc.to_dict()
        record["_doc_id"] = doc.id
        records.append(record)
    return records


def add_mrb_device(db, device: dict, collection: str = MRB_COLLECTION) -> None:
    """Record a device sent to MRB (scrap) - keyed by qr like devices/
    completed (one live document per device), since a given QR is only
    ever scrapped once."""
    db.collection(collection).document(device["qr"]).set(device)


def delete_mrb_devices(db, qrs, collection: str = MRB_COLLECTION) -> None:
    _delete_from(db, collection, qrs)


def listen_mrb_devices(db, on_change, collection: str = MRB_COLLECTION):
    """Same as listen_devices, but for an MRB-devices-shaped collection."""

    def _callback(col_snapshot, _changes, _read_time):
        on_change([doc.to_dict() for doc in col_snapshot])

    return db.collection(collection).on_snapshot(_callback)


def listen_io_queue(db, on_change, departments: list, collection: str = IO_QUEUE_COLLECTION):
    """Live listener for one app mode's slice of the department scan
    queue - e.g. just "hard-test" for Debug, or the other four
    departments together for Assembly Rework (see app_gui.py's
    _DEBUG_QUEUE_DEPARTMENTS/_AR_QUEUE_DEPARTMENTS). Unlike
    online_repairs, this needs no time-based bounding: a queue entry is
    removed the moment Online Repair picks it up (see
    remove_io_queue_entries), so the collection stays small on its own -
    it's used every day same as Online Repair (see
    ONLINE_REPAIR_LIVE_WINDOW_DAYS's docstring for that distinction)."""

    def _callback(col_snapshot, _changes, _read_time):
        records = []
        for doc in col_snapshot:
            record = doc.to_dict()
            record["_doc_id"] = doc.id
            records.append(record)
        on_change(records)

    query = db.collection(collection).where(filter=FieldFilter("department", "in", departments))
    return query.on_snapshot(_callback)


def delete_io_queue_entries(db, doc_ids, collection: str = IO_QUEUE_COLLECTION) -> None:
    _delete_from(db, collection, doc_ids)


def remove_io_queue_entries(db, qr: str, collection: str = IO_QUEUE_COLLECTION) -> None:
    """Delete every io_queue entry for this QR (any department) - called
    when an Online Repair scan picks the unit back up out of whichever
    department queue it was sitting in. Queried by the `qr` field rather
    than a known doc id since Online Repair doesn't know which department
    queued it (or whether it was queued at all)."""
    docs = list(db.collection(collection).where(filter=FieldFilter("qr", "==", qr)).stream())
    if not docs:
        return
    batch = db.batch()
    for doc in docs:
        batch.delete(doc.reference)
    batch.commit()


def add_qr_change(db, record: dict, collection: str = QR_CHANGES_COLLECTION) -> None:
    """Record one tracked PCB - keyed by pcb_qr (the permanent identifier),
    not the paper QR, so re-scanning the same physical PCB updates its
    existing row instead of creating a duplicate."""
    db.collection(collection).document(record["pcb_qr"]).set(record)


def update_qr_change_current(db, pcb_qr: str, current_qr: str, collection: str = QR_CHANGES_COLLECTION) -> None:
    """Record a detected paper-QR reassignment for one already-tracked PCB
    (see app_gui.py's Refresh handling for the qr_change view) - only ever
    touches the current_qr field, leaving previous_qr as the original
    baseline it's being compared against."""
    db.collection(collection).document(pcb_qr).update({"current_qr": current_qr})


def delete_qr_changes(db, pcb_qrs, collection: str = QR_CHANGES_COLLECTION) -> None:
    _delete_from(db, collection, pcb_qrs)


def listen_qr_changes(db, on_change, collection: str = QR_CHANGES_COLLECTION):
    """Same as listen_devices, but for the qr_changes collection."""

    def _callback(col_snapshot, _changes, _read_time):
        on_change([doc.to_dict() for doc in col_snapshot])

    return db.collection(collection).on_snapshot(_callback)


def log_lookup_failure(db, record: dict) -> None:
    """Record one "WebDB board lookup came back empty after every retry"
    event - append-only log (auto-generated doc id, since the same QR can
    legitimately fail more than once over time). Best-effort: never let a
    problem writing the diagnostic itself interfere with the scan the
    operator is actually trying to do, so callers should fire this from a
    background thread and not treat failures here as user-facing errors.
    """
    db.collection(LOOKUP_FAILURES_COLLECTION).document().set(record)


def log_sheet_write_failure(db, record: dict) -> None:
    """Record one "PROD5 sheet write failed even after retrying" event -
    same append-only, best-effort shape as log_lookup_failure above. The
    scan itself already succeeded (the Firestore record exists, the
    operator sees it on screen) - this is purely so a sheet write that was
    silently lost before (a bare background thread with no error handling)
    is now at least discoverable after the fact, the same way a WebDB
    lookup failure already is.
    """
    db.collection(SHEET_WRITE_FAILURES_COLLECTION).document().set(record)


def hash_password(password: str) -> str:
    """App-account passwords are hashed before being stored in Firestore -
    a shared, multi-user store that more people can read than just the
    account owner, unlike the single local session.json file."""
    return hashlib.sha256((_PASSWORD_SALT + password).encode("utf-8")).hexdigest()


def get_app_account(db, username: str):
    doc = db.collection(ACCOUNTS_COLLECTION).document(username).get()
    return doc.to_dict() if doc.exists else None


def set_app_account(db, username: str, password: str, role: str, mode_permission: str = "both") -> None:
    """Create or fully overwrite an account (used for new accounts).

    `mode_permission` is "debug", "assembly_rework", or "both" - which
    app_mode(s) (see app_gui.py) this account is allowed to use. Accounts
    created before this field existed are treated as "both" wherever this
    is read (see app_gui.py's login handling), so nobody's access silently
    narrows just because the field is missing.
    """
    db.collection(ACCOUNTS_COLLECTION).document(username).set({
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "mode_permission": mode_permission,
    })


def set_account_role(db, username: str, role: str) -> None:
    """Change an existing account's role without touching its password."""
    db.collection(ACCOUNTS_COLLECTION).document(username).update({"role": role})


def set_account_mode(db, username: str, mode_permission: str) -> None:
    """Change an existing account's mode_permission without touching its
    password or role."""
    db.collection(ACCOUNTS_COLLECTION).document(username).update({"mode_permission": mode_permission})


def delete_app_account(db, username: str) -> None:
    db.collection(ACCOUNTS_COLLECTION).document(username).delete()


def listen_app_accounts(db, on_change):
    """Subscribe to live changes on the app_accounts collection, so every
    connected client's role/permissions update immediately when the owner
    or an admin edits accounts - no re-login required."""

    def _callback(col_snapshot, _changes, _read_time):
        on_change([doc.to_dict() for doc in col_snapshot])

    return db.collection(ACCOUNTS_COLLECTION).on_snapshot(_callback)


def get_queue_account(db, username: str):
    doc = db.collection(QUEUE_ACCOUNTS_COLLECTION).document(username).get()
    return doc.to_dict() if doc.exists else None


def set_queue_account(db, username: str, password: str, department: str) -> None:
    """Create or fully overwrite a department scan-page login (Settings ->
    Queue Registration). Separate collection from app_accounts - these
    accounts only ever sign in from the public web pages (public/*/index.html
    via functions/index.js's queueLogin), never into the desktop app itself,
    and are restricted to exactly one `department` rather than a role/mode
    permission pair. Same password hashing as app accounts (see
    hash_password) for consistency; queueLogin replicates the same
    salt+sha256 scheme in JS to verify it.
    """
    db.collection(QUEUE_ACCOUNTS_COLLECTION).document(username).set({
        "username": username,
        "password_hash": hash_password(password),
        "department": department,
    })


def set_queue_account_department(db, username: str, department: str) -> None:
    """Change an existing queue account's department without touching its password."""
    db.collection(QUEUE_ACCOUNTS_COLLECTION).document(username).update({"department": department})


def delete_queue_account(db, username: str) -> None:
    db.collection(QUEUE_ACCOUNTS_COLLECTION).document(username).delete()


def listen_queue_accounts(db, on_change):
    """Same as listen_app_accounts, but for queue_accounts - only meant to
    be subscribed while Settings' Queue Registration tab is open (see
    app_gui.py), not for the whole session, since no other part of the
    desktop app needs this collection live."""

    def _callback(col_snapshot, _changes, _read_time):
        on_change([doc.to_dict() for doc in col_snapshot])

    return db.collection(QUEUE_ACCOUNTS_COLLECTION).on_snapshot(_callback)
