"""Shared helper: push completed write_retry requests past the terminal retention limit.

The properties under test ("a pruned original receipt still replays/undoes
correctly") need the retention limit exceeded, not 500 real iterations, so the
limit is patched small. ``tests.test_write_retry`` or the durable test keep the
real constant pinned in one cheap place.
"""
from unittest import mock

from eutonos import write_retry

PATCHED_RETENTION = 3


def patched_retention(limit: int = PATCHED_RETENTION):
    return mock.patch.object(write_retry, "TERMINAL_RETENTION", limit)


def complete_past_limit(test, host, root, summary: str, limit: int = PATCHED_RETENTION) -> set:
    """Enqueue and run ``limit + 1`` board updates; assert each completes. Call under patched_retention."""
    test.assertEqual(write_retry.TERMINAL_RETENTION, limit)
    ids = set()
    for index in range(limit + 1):
        payload = {"owner_path": "docs/WORK_ITEMS.md", "item_id": "LIVE",
                   "changes": {"summary": f"{summary} {index}"}}
        queued = write_retry.enqueue(host, root, "writer", "board-update", payload)
        ids.add(queued["request_id"])
        completed = write_retry.run_once(host, root)
        test.assertEqual((completed["request_id"], completed["outcome"]), (queued["request_id"], "complete"), completed)
        db = write_retry._connect(host, readonly=True)
        try:
            test.assertEqual(db.execute("SELECT state FROM requests WHERE id=?", (queued["request_id"],)).fetchone()[0], "complete")
        finally:
            db.close()
    test.assertEqual(len(ids), limit + 1)
    return ids
