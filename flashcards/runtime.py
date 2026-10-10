"""Workspace isolation, atomic checkpoints, and the single-writer contract."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import paths


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".checkpoint-", delete=False
    ) as fh:
        tmp = Path(fh.name)
        try:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


_local = threading.local()


@contextmanager
def writer_lock(database: Path | None = None):
    """A reentrant process lock shared by all mutating CLI services.

    Keep it held across external work: leases alone cannot prevent an expired
    job's provider process from publishing a late result.
    """
    path = Path(str(database or paths.DB_PATH) + ".lock")
    held = getattr(_local, "held", set())
    if path in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Another flashcards writer is active ({path}). Retry when it finishes."
            ) from exc
        _local.held = held | {path}
        try:
            yield
        finally:
            _local.held = held
            fcntl.flock(fh, fcntl.LOCK_UN)


def run_process(cmd, *, input=None, timeout=60, cwd=None, stop=None):
    """Run a provider in its own process group; cancel children on interruption."""
    import signal

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            if stop is not None and stop.is_set():
                raise KeyboardInterrupt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                stdout, stderr = proc.communicate(input=input if first else None, timeout=min(1, remaining))
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
