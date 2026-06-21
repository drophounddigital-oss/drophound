"""Database backup and rollback strategy.

Before any destructive migration, call `backup(conn, db_path)` to write a
timestamped copy. If something goes wrong, `restore(backup_path, db_path)`
swaps it back in.

CLI usage (via drophound/cli.py):
    python -m drophound backup          # write backup now
    python -m drophound restore <file>  # rollback to a backup
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("drophound")


def backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    """Write a point-in-time copy of the DB using SQLite's online backup API."""
    db_path = Path(db_path)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = db_path.parent / "backups" / f"drophound_{ts}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)

    dest_conn = sqlite3.connect(str(dest))
    try:
        conn.backup(dest_conn)
        dest_conn.close()
    except Exception:
        dest_conn.close()
        raise

    logger.info("backup written -> %s", dest)
    _prune_old(dest.parent, keep=10)
    return dest


def restore(backup_path: Path, db_path: Path) -> None:
    """Replace the live DB with `backup_path`. The old live file is moved aside."""
    backup_path = Path(backup_path)
    db_path = Path(db_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")
    aside = db_path.with_suffix(f".pre_restore_{int(datetime.now(timezone.utc).timestamp())}.db")
    shutil.move(str(db_path), str(aside))
    shutil.copy2(str(backup_path), str(db_path))
    logger.info("restored %s -> %s (old db at %s)", backup_path, db_path, aside)


def list_backups(db_path: Path) -> list[Path]:
    d = Path(db_path).parent / "backups"
    if not d.exists():
        return []
    return sorted(d.glob("drophound_*.db"), reverse=True)


def _prune_old(backup_dir: Path, keep: int) -> None:
    all_backups = sorted(backup_dir.glob("drophound_*.db"), reverse=True)
    for old in all_backups[keep:]:
        old.unlink(missing_ok=True)
        logger.info("pruned old backup: %s", old.name)
