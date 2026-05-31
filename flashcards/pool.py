"""Tiny thread-pool helper with progress output, used by every mode."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_pool(
    items: list[T],
    work_fn: Callable[[T], R],
    *,
    workers: int = 10,
    label: str = "items",
    progress_every: int = 50,
    describe: Callable[[T], str] | None = None,
) -> Iterator[tuple[T, R | Exception]]:
    """Run ``work_fn`` against each item in parallel, yielding (item, result | exc).

    Errors are caught and returned as the result so callers decide how to react;
    they are also printed inline.
    """
    if not items:
        return
    total = len(items)
    print(f"  Processing {total} {label} with {workers} workers...", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work_fn, it): it for it in items}
        for fut in as_completed(futs):
            done += 1
            it = futs[fut]
            exc = fut.exception()
            if exc is not None:
                desc = describe(it) if describe else repr(it)
                print(f"  [{done}/{total}] {desc}: ERROR: {exc}", flush=True)
                yield it, exc  # type: ignore[misc]
            else:
                yield it, fut.result()
            if done % progress_every == 0:
                print(f"  Progress: {done}/{total}", flush=True)
