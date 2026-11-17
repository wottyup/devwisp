"""Move the complete Linux state directory when adopting the DevWisp name."""

from __future__ import annotations

import os
from pathlib import Path


def migrate_legacy_state_directory(target: Path) -> bool:
    # An explicit deployment path must never cause a different directory to move.
    if os.name != "posix" or any(os.environ.get(key) for key in (
        "DEVWISP_AUTH_FILE", "DEVWISP_HOME", "DEVWISP_STATE_FILE",
    )):
        return False
    if target.name != "devwisp" or target.exists() or target.is_symlink():
        return False
    source = target.with_name("codex-remote")
    if source.is_symlink() or not source.is_dir():
        return False
    # Startup runs after the previous backend exits, so no database writers
    # remain. Moving the whole directory also preserves private gateway snapshots,
    # SSH settings, and attachments without reading or copying their contents.
    source.rename(target)
    try:
        # Historical messages contain absolute attachment paths under this name.
        source.symlink_to(target.name, target_is_directory=True)
    except OSError:
        target.rename(source)
        raise
    return True
