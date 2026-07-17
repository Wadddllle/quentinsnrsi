"""In-memory mutation log for the live editing session (Phase 3 of the
project plan: remove-building + versioning). Tracks what's changed since the
last Save Version, for two purposes: undo (reverse the last op) and building
a human-readable changelog for the version-control commit message.

Deliberately NOT persisted to disk and NOT `sbg/edit.py`'s concern -- this is
purely an `sbg/ui/` bookkeeping layer on top of the already-proven
add_building/remove_building functions. Undo boundary is the last save (see
Session.clear(), called by /api/versions/save): going further back than the
current session is version restore's job (sbg/ui/versioning.py), not this
stack's -- keeps undo simple/linear and avoids needing anything disk-backed
here.

remove_building(cm, id, compact=False) never returns the deleted CityObject
(see sbg/edit.py) and never touches cm["vertices"] in compact=False mode --
so the router captures the CityObject dict itself (by reference, not a deep
copy) as the undo snapshot before calling remove_building(). This is safe
because remove_building only ever does `del cm["CityObjects"][id]`, never
mutates a CityObject dict's own contents in place, and nothing else in the
live server process touches an existing CityObject dict's contents after
load (only offline OneMap backfill scripts do that, run via CLI, never
inside this process) -- so the captured reference stays exactly what it was
at capture time.
"""
import time


class MutationLogEntry:
    __slots__ = ("op", "building_id", "timestamp", "snapshot")

    def __init__(self, op, building_id, snapshot=None):
        self.op = op  # "remove" | "add"
        self.building_id = building_id
        self.timestamp = time.time()
        self.snapshot = snapshot  # the removed CityObject dict (for undo), or None for "add"

    def to_dict(self):
        return {"op": self.op, "building_id": self.building_id, "timestamp": self.timestamp}


class Session:
    def __init__(self):
        self.mutation_log = []

    def record(self, op, building_id, snapshot=None):
        self.mutation_log.append(MutationLogEntry(op, building_id, snapshot))

    def pop_last(self):
        """Removes and returns the most recent entry, or None if the log is empty."""
        if not self.mutation_log:
            return None
        return self.mutation_log.pop()

    def summary(self):
        """Groups the log by op for a one-line commit subject, e.g.
        "3 buildings removed, 1 added".
        """
        counts = {}
        for entry in self.mutation_log:
            counts[entry.op] = counts.get(entry.op, 0) + 1
        if not counts:
            return "No changes"
        labels = {"remove": "removed", "add": "added"}
        parts = [f"{n} building{'s' if n != 1 else ''} {labels.get(op, op)}" for op, n in counts.items()]
        return ", ".join(parts)

    def commit_message(self, note=None):
        subject = self.summary()
        if note:
            subject = f"{note} ({subject})"
        body_lines = [
            f"{e.op}  {e.building_id}  {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(e.timestamp))}"
            for e in self.mutation_log
        ]
        return subject + ("\n\n" + "\n".join(body_lines) if body_lines else "")

    def to_status_dict(self):
        return {
            "dirty": bool(self.mutation_log),
            "count": len(self.mutation_log),
            "summary": self.summary(),
            "log": [e.to_dict() for e in self.mutation_log],
        }

    def clear(self):
        self.mutation_log = []
