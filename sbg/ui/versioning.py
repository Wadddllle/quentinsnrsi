"""Version control for data/sbg.city.json (Phase 3 of the project plan).
Not HTTP-facing -- sbg/ui/routers/versions.py wraps this.

A SECOND, independent git repo rooted at data/, entirely separate from the
main project repo -- data/ is excluded from the main repo via .gitignore
(`/data/`, confirmed), and this mirrors the existing convention already used
for ninja/, buildings.city/, onemap-slicer/ (each vendored with its own
.git, none tracked by the main repo). Never touches the main repo's git
state in any way.

Only ever operates on one explicit path (sbg.city.json) -- never `git add
-A`/`git add .`. This matters concretely here: data/ is 5.5GB and already
holds a 525MB STL, a 212MB JSONL, several ~185MB manual backup copies,
.tif/.npz/.obj pipeline outputs, cutouts/, experiments/, onemap_cache/ --
exactly the kind of large files an accidental blanket `git add` would
irreversibly commit. data/.gitignore is written as a strict ALLOWLIST (`*`
/ `!.gitignore` / `!sbg.city.json`), not a denylist of known-bad patterns,
specifically so a forgotten extension can't cause that mistake -- a denylist
approach is one new file type away from silently committing gigabytes.

restore_version() deliberately uses `git checkout <hash> -- <file>` (a
single-path checkout), never `git checkout <hash>` or a branch/reset. HEAD
never detaches, so there is no merge-shaped operation reachable through this
module even in principle -- "no branches/merges ever exposed" isn't a UI
restraint here, it's structural: restoring an old version just makes the
working tree differ from HEAD (the old content), and the next save_version()
is an ordinary linear commit showing "restored to Nm ago" as an ordinary
diff from current HEAD.

Commit messages are NOT a diff of the JSON (meaningless here anyway --
subset_cityjson/vertex-pool remapping would make a text diff unreadable) --
the caller (routers/versions.py) builds the message from the in-memory
Session's mutation log (see session.py), which is the real, human-readable
changelog the user asked for. `git log` becomes the "history" UI directly.
"""
import subprocess
from pathlib import Path

GITIGNORE_HEADER = ["*", "!.gitignore"]

# NUL-delimited fields within a commit, RS (0x1e)-delimited between commits --
# safe against commit subjects/bodies containing any other character,
# including newlines (bodies are multi-line by construction, see
# session.py's commit_message()).
_LOG_FORMAT = "%H%x00%aI%x00%s%x00%b%x1e"


def _run(data_dir, *args):
    result = subprocess.run(
        ["git", "-C", str(data_dir), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def ensure_repo(data_dir, filename):
    """filename is allowlisted into data/.gitignore (appended if not already
    present, never removed) -- not hardcoded to "sbg.city.json" alone,
    since --dataset (sbg/ui/__main__.py) lets the server load a different
    fixture (e.g. a small test file for fast iteration), and that fixture
    needs its own allow-entry the first time IT gets a real save, not just
    the production file's name. Still a strict allowlist overall (`*` deny
    by default) -- adding more specific `!name` entries over time doesn't
    weaken that, it just means more than one known file is trackable.
    """
    data_dir = Path(data_dir)
    if not (data_dir / ".git").is_dir():
        _run(data_dir, "init")
    gitignore = data_dir / ".gitignore"
    lines = list(GITIGNORE_HEADER)
    if gitignore.exists():
        existing = [ln for ln in gitignore.read_text().splitlines() if ln.strip()]
        for ln in existing:
            if ln not in lines:
                lines.append(ln)
    allow_entry = f"!{filename}"
    if allow_entry not in lines:
        lines.append(allow_entry)
    content = "\n".join(lines) + "\n"
    if not gitignore.exists() or gitignore.read_text() != content:
        gitignore.write_text(content)


def _has_any_commits(data_dir):
    result = subprocess.run(
        ["git", "-C", str(data_dir), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def ensure_baseline(data_dir, filename):
    """Call this at server STARTUP, immediately after loading `filename`
    from disk and before any endpoint can mutate the in-memory working copy
    -- NOT lazily on first save. Guarantees the very first git commit always
    captures the true pristine on-disk state.

    Real incident this fixes: save_version() used to create this same
    "Baseline import" commit lazily, the first time /api/versions/save was
    ever called -- from whatever was CURRENTLY on disk at that moment. A
    user who removed a building before ever clicking Save Version for the
    first time got a "baseline" that already reflected the removal, with no
    earlier git snapshot to fall back to. The building was only recoverable
    because an unrelated manual backup file happened to still exist from
    earlier pipeline work -- not something this tool should ever depend on.
    """
    data_dir = Path(data_dir)
    ensure_repo(data_dir, filename)
    if not _has_any_commits(data_dir):
        _run(data_dir, "add", "--", filename)
        _run(data_dir, "commit", "-m", f"Baseline import ({filename})")


def save_version(data_dir, filename, message):
    """Commits the current state of `filename` (relative to data_dir).
    Returns {hash, subject}.

    Does NOT create the baseline commit itself -- see ensure_baseline()
    above, called once at server startup. The _has_any_commits() check below
    is a defensive fallback only (e.g. an older server process still running
    without the startup call), not the primary mechanism anymore.
    """
    data_dir = Path(data_dir)
    ensure_repo(data_dir, filename)
    _run(data_dir, "add", "--", ".gitignore")

    if not _has_any_commits(data_dir):
        _run(data_dir, "add", "--", filename)
        _run(data_dir, "commit", "-m", f"Baseline import ({filename})")

    _run(data_dir, "add", "--", filename)
    # Nothing to commit is a real, valid outcome (e.g. Save Version pressed
    # with an empty session) -- not an error, just report the current HEAD.
    status = _run(data_dir, "status", "--porcelain", "--", filename)
    if not status.strip():
        head_hash = _run(data_dir, "rev-parse", "HEAD").strip()
        subject = _run(data_dir, "log", "-1", "--pretty=%s", head_hash).strip()
        return {"hash": head_hash, "subject": subject, "created": False}

    _run(data_dir, "commit", "-m", message)
    head_hash = _run(data_dir, "rev-parse", "HEAD").strip()
    subject = message.splitlines()[0]
    return {"hash": head_hash, "subject": subject, "created": True}


def list_versions(data_dir):
    data_dir = Path(data_dir)
    if not (data_dir / ".git").is_dir() or not _has_any_commits(data_dir):
        return []
    raw = _run(data_dir, "log", f"--pretty=format:{_LOG_FORMAT}")
    versions = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        commit_hash, date, subject, body = record.split("\x00")
        versions.append({"hash": commit_hash, "date": date, "subject": subject, "body": body.strip()})
    return versions


def restore_version(data_dir, commit_hash, filename):
    """Checks out `filename` as it existed at `commit_hash`, into the
    working tree, WITHOUT moving HEAD or creating a branch (see module
    docstring). The caller is responsible for reloading the file into
    memory afterward -- this only touches the file on disk.
    """
    data_dir = Path(data_dir)
    _run(data_dir, "checkout", commit_hash, "--", filename)
