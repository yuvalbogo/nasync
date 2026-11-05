"""Random histories: whatever a person does to a folder, the mirror follows.

Each seed builds a tree, then applies a sequence of the operations people
actually perform — edits that keep the size, older versions put back, folder
and case renames, deletes, touches — syncing after each and checking the NAS
against the folder byte for byte. It finishes with a restore into an empty
folder, which must give back exactly what was there.

A failure prints its seed and the history, so it can be replayed exactly:

    NASYNC_LAB_SEED=1234 pytest --lab tests/lab/test_random_history.py
"""

import os
import random
import shutil
import time

import pytest

SEEDS = ([int(os.environ["NASYNC_LAB_SEED"])] if os.environ.get("NASYNC_LAB_SEED")
         else [11, 23, 37, 41, 59])
STEPS = int(os.environ.get("NASYNC_LAB_STEPS", "12"))
NAMES = ["notes.txt", "photo.jpg", "פרויקט.docx", "a b.txt", "index.html",
         "README", "data.bin", "Ünïcödé.txt"]
DIRS = ["", "docs", "docs/old", "Photos/2024", "פרוייקטים", "deep/er/est"]


def _files(base):
    out = []
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), base))
    return sorted(out)


def _dirs(base):
    out = []
    for dirpath, dirs, _files in os.walk(base):
        for name in dirs:
            out.append(os.path.relpath(os.path.join(dirpath, name), base))
    return sorted(out)


def _content(rng, size=None):
    size = rng.choice([0, 5, 64, 4096]) if size is None else size
    return bytes(rng.getrandbits(8) for _ in range(size))


def _write(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _fresh_name(rng, base, existing_folded):
    for _ in range(50):
        rel = os.path.join(rng.choice(DIRS), f"{rng.randint(0, 99)}-{rng.choice(NAMES)}")
        if rel.casefold() not in existing_folded:
            return rel
    return None


def _step(rng, base, history):
    """Apply one random real-world operation; return its description."""
    files = _files(base)
    folded = {f.casefold() for f in files}
    ops = ["create", "create"]
    if files:
        ops += ["edit_same_size", "edit_resize", "older_version", "delete",
                "rename", "case_rename", "touch", "move_folder"]
    op = rng.choice(ops)
    now = time.time()
    if op == "create":
        rel = _fresh_name(rng, base, folded)
        if rel:
            _write(os.path.join(base, rel), _content(rng))
        return f"create {rel}"
    rel = rng.choice(files)
    path = os.path.join(base, rel)
    size = os.path.getsize(path)
    if op == "edit_same_size":
        _write(path, _content(rng, size), mtime=now + 5)
    elif op == "edit_resize":
        _write(path, _content(rng, size + rng.randint(1, 50)), mtime=now + 5)
    elif op == "older_version":
        _write(path, _content(rng, size), mtime=now - rng.randint(2, 900) * 86400)
    elif op == "delete":
        os.remove(path)
    elif op == "touch":
        os.utime(path, (now + 7, now + 7))
    elif op == "rename":
        target = _fresh_name(rng, base, folded)
        if not target:
            return "rename skipped"
        os.makedirs(os.path.dirname(os.path.join(base, target)), exist_ok=True)
        os.rename(path, os.path.join(base, target))
        return f"rename {rel} -> {target}"
    elif op == "case_rename":
        head, name = os.path.split(rel)
        swapped = name.swapcase()
        if swapped == name or os.path.join(head, swapped).casefold() != rel.casefold():
            return "case_rename skipped"
        os.rename(path, os.path.join(base, head, swapped))
        return f"case_rename {rel} -> {swapped}"
    elif op == "move_folder":
        folders = [d for d in _dirs(base) if os.listdir(os.path.join(base, d))]
        if not folders:
            return "move_folder skipped"
        folder = rng.choice(folders)
        target = folder + f"-moved{rng.randint(0, 9)}"
        if os.path.exists(os.path.join(base, target)):
            return "move_folder skipped"
        os.rename(os.path.join(base, folder), os.path.join(base, target))
        return f"move_folder {folder} -> {target}"
    return f"{op} {rel}"


@pytest.mark.parametrize("seed", SEEDS)
def test_a_random_history_is_mirrored_exactly(lab, seed):
    rng = random.Random(seed)
    base = lab.pair("h")
    for _ in range(12):
        rel = _fresh_name(rng, base, {f.casefold() for f in _files(base)})
        if rel:
            _write(os.path.join(base, rel), _content(rng))
    history = ["initial tree"]
    lab.sync("h", allow_mass_delete=True)
    lab.assert_mirrored("h")
    for step in range(STEPS):
        history.append(_step(rng, base, history))
        result = lab.sync("h", allow_mass_delete=True)
        try:
            lab.assert_mirrored("h")
            assert not result.get("failed") and not result.get("errors"), lab.log
        except AssertionError as error:
            raise AssertionError(f"seed {seed}, step {step}:\n  "
                                 + "\n  ".join(history) + f"\n{error}") from None

    # And the backup gives it all back.
    before = lab.local_tree("h")
    shutil.rmtree(base)
    os.makedirs(base)
    lab.restore("h")
    assert lab.local_tree("h") == before, f"seed {seed}: restore differs"
