"""Saving and restoring crawl state for pause/resume."""

from __future__ import annotations

import json
import os
import pickle
import time
from pathlib import Path
from typing import Any

from ..errors import CheckpointError

STATE_FILE = "state.pickle"
SUMMARY_FILE = "summary.json"
FORMAT_VERSION = 1


class Checkpoint:
    """Crawl state stored in a directory (``crawl_dir``).

    ``state.pickle`` holds pending requests, seen-URL fingerprints, stats and
    throttle state. It is written atomically, so a crash mid-write never
    corrupts the previous checkpoint. Only load checkpoints you created:
    like any pickle, the file is trusted input.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / STATE_FILE

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, state: dict[str, Any]) -> None:
        state = {**state, "version": FORMAT_VERSION, "saved_at": time.time()}
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "wb") as fh:
                pickle.dump(state, fh, protocol=pickle.HIGHEST_PROTOCOL)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except (OSError, pickle.PicklingError, TypeError, AttributeError) as exc:
            tmp.unlink(missing_ok=True)
            raise CheckpointError(f"Could not save crawl state to {self.path}: {exc}") from exc

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with open(self.path, "rb") as fh:
                state = pickle.load(fh)
        except Exception as exc:
            raise CheckpointError(f"Could not read crawl state from {self.path}: {exc}") from exc
        if not isinstance(state, dict) or state.get("version") != FORMAT_VERSION:
            raise CheckpointError(f"{self.path} was written by an incompatible version of wintergrab")
        return state

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def write_summary(self, summary: dict[str, Any]) -> None:
        (self.dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
