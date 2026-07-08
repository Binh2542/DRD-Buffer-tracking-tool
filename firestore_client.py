from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

DEVICES_COLLECTION = "devices"


def init_firestore(key_path: Path):
    """Initialize (once per process) and return a Firestore client."""
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(key_path))
        firebase_admin.initialize_app(cred)
    return firestore.client()


def add_device(db, device: dict) -> None:
    db.collection(DEVICES_COLLECTION).document(device["qr"]).set(device)


def delete_devices(db, qrs) -> None:
    batch = db.batch()
    for qr in qrs:
        batch.delete(db.collection(DEVICES_COLLECTION).document(qr))
    batch.commit()


def list_devices(db) -> list[dict]:
    return [doc.to_dict() for doc in db.collection(DEVICES_COLLECTION).stream()]


def listen_devices(db, on_change):
    """Subscribe to live changes on the devices collection.

    `on_change` is called with the full current list of devices, both once
    immediately (initial snapshot) and again on every add/update/delete from
    any connected client. Returns a watch handle - call .unsubscribe() on it
    to stop listening (e.g. on logout/close).
    """

    def _callback(col_snapshot, _changes, _read_time):
        on_change([doc.to_dict() for doc in col_snapshot])

    return db.collection(DEVICES_COLLECTION).on_snapshot(_callback)
