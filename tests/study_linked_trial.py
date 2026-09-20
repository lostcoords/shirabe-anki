"""Keep a loopback-only development trial and sync it through the native coordinator."""

import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def configuration(directory):
    from urllib.parse import urlsplit

    requested = Path(directory)
    root = requested.resolve(strict=True)
    scratch = Path(__file__).resolve().parents[2] / "tmp"
    if requested.is_symlink() or not root.is_relative_to(scratch) or not root.name.startswith("study-linked-trial-"):
        raise ValueError("Use a retained study-linked-trial directory under workspace tmp")
    config = json.loads((root / "trial.json").read_text())
    origin = urlsplit(config["origin"])
    if config["kind"] != "shirabe-linked-development-trial" or origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        raise ValueError("The retained trial must use its literal loopback development origin")
    package = Path(config["package"]).resolve(strict=True)
    if not package.is_relative_to(scratch) or package.suffix != ".apkg":
        raise ValueError("The trial package must remain under workspace tmp")
    if digest(package) != config["package_sha256"]:
        raise ValueError("The saved trial package changed")
    collection = root / "collection.anki2"
    if collection.is_symlink() or collection.exists() and (not collection.is_file() or collection.stat().st_nlink != 1):
        raise ValueError("The retained collection must be a private regular file")
    return {**config, "directory": str(root), "package": str(package), "retained_trial": True}


def exercise(mw, session, wait_until, config):
    from aqt.qt import Qt
    from shirabe_addon.sync_state import SyncState
    from shirabe_addon.sync_ui import StudySyncDialog
    from sync_http_roundtrip import authorize, emit
    from sync_mixed_pairing_roundtrip import prepare

    reported = None

    def report():
        nonlocal reported
        if session.status and session.status != reported:
            reported = session.status
            emit({"stage": "trial_progress", "status": reported})

    session.changed.connect(report)
    with SyncState(session.path) as state:
        captured = state.get("initial_snapshot")
    if not captured and mw.col.note_count() == 0:
        prepare(mw.col, config["package"])
    if not session.login.credentials():
        authorize(session.login)
    dialog = StudySyncDialog(mw, config["origin"])
    wait_until(lambda: not session.busy)
    if not captured or captured["phase"] != "uploaded":
        selected = captured["root_deck_ids"] if captured else mw.col.db.list("SELECT DISTINCT did FROM cards ORDER BY did")
        for index in range(dialog.deck_list.count()):
            item = dialog.deck_list.item(index)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in selected else Qt.CheckState.Unchecked)
        assert dialog.upload.isEnabled()
        dialog.upload.click()
        wait_until(lambda: not session.busy, timeout=180)
    if config.get("review_card"):
        def review(col):
            card = col.get_card(config["review_card"])
            card.start_timer()
            return col.sched.answerCard(card, 3)

        assert session.run(review, lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
    assert session.start()
    wait_until(lambda: not session.busy, timeout=180)
    assert session.status == "Saved changes exchanged."
    with SyncState(session.path) as state:
        assert not state.pending()
        assert state.get("delivery") is None and state.get("changes") is None
        profile_id = state.get("initial_snapshot")["profile"]["id"]
    emit({"stage": "trial_ready", "profile_id": profile_id, "notes": mw.col.note_count(),
          "cards": mw.col.db.all("SELECT id, reps FROM cards ORDER BY id"),
          "history": mw.col.db.scalar("SELECT count(*) FROM revlog"),
          "directory": config["directory"]})
    dialog.reject()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--review-card", type=int)
    args = parser.parse_args()
    config = configuration(args.directory)
    if args.review_card is not None:
        if args.review_card <= 0:
            parser.error("Choose a positive Anki card ID from this disposable trial")
        config["review_card"] = args.review_card
    from sync_session_roundtrip import run

    run(config)
