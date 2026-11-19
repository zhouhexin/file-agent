"""Atomic snapshot writes shared by manual imports and automatic synchronization."""

from datetime import datetime, timezone


def state(db, key):
    row = db.execute("SELECT value FROM portal_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(db, key, value):
    db.execute("INSERT INTO portal_state(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def save_snapshot(db, *, filename, digest, actor, people, source_key=""):
    """Caller owns the transaction; pointer and all person/detail rows commit together."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = db.execute("INSERT INTO funding_snapshots(filename,sha256,imported_by,imported_at,person_count) VALUES(?,?,?,?,?)",
                        (filename, digest, actor, now, len(people)))
    snapshot_id = cursor.lastrowid
    for person in people:
        db.execute("INSERT INTO funding_people(snapshot_id,name,sequence,research_total,other_total,total_spent,remaining,needs_review) VALUES(?,?,?,?,?,?,?,?)",
                   (snapshot_id, person["name"], person["sequence"], person["research_total"],
                    person["other_total"], person["total_spent"], person["remaining"], int(person["needs_review"])))
        for detail in person["details"]:
            db.execute("INSERT INTO funding_details(snapshot_id,name,sequence,registration,date_text,usage_amount,note,needs_review) VALUES(?,?,?,?,?,?,?,?)",
                       (snapshot_id, person["name"], detail["sequence"], detail["registration"],
                        detail["date"], detail["usage_amount"], detail["note"], int(detail["needs_review"])))
    set_state(db, "funding_snapshot_id", snapshot_id)
    set_state(db, "funding_source_key", source_key)
    db.execute("INSERT INTO audit_events(actor,action,subject,created_at) VALUES(?,?,?,?)",
               (actor, "FUNDING_IMPORTED", str(snapshot_id), now))
    return snapshot_id
