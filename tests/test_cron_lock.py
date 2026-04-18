"""PID lock must detect stale locks older than MAX_LOCK_AGE_S and reclaim them."""
import os
import time

import pytest


def test_lock_reclaim_when_older_than_max_age(tmp_path, monkeypatch):
    from cron import common

    # Redirect lock file into tmp_path so we don't collide with a real lock
    lock = tmp_path / "tradebot_test.lock"
    monkeypatch.setattr(common, "_lock_path_for", lambda name: str(lock))
    # Swallow the Telegram alert that fires on stale-lock eviction so the test
    # doesn't attempt a real HTTP call
    monkeypatch.setattr(common, "send_telegram", lambda msg: None)

    # Write a stale lock: a process that looks alive (use parent PID) but with
    # a very old mtime
    lock.write_text(str(os.getppid()))
    old_time = time.time() - (common.MAX_LOCK_AGE_S + 60)
    os.utime(lock, (old_time, old_time))

    # acquire_lock should reclaim (not sys.exit)
    common.acquire_lock("test")

    # Lock should now contain our current PID
    assert int(lock.read_text().strip()) == os.getpid()
    common.release_lock("test")


def test_lock_blocks_when_recent_and_alive(tmp_path, monkeypatch):
    from cron import common

    lock = tmp_path / "tradebot_test.lock"
    monkeypatch.setattr(common, "_lock_path_for", lambda name: str(lock))
    monkeypatch.setattr(common, "send_telegram", lambda msg: None)

    lock.write_text(str(os.getppid()))  # alive PID
    # Fresh mtime
    os.utime(lock, None)

    with pytest.raises(SystemExit):
        common.acquire_lock("test")


def test_lock_reclaim_when_pid_is_dead(tmp_path, monkeypatch):
    """A lock held by a PID that no longer exists should be reclaimed cleanly."""
    from cron import common

    lock = tmp_path / "tradebot_test.lock"
    monkeypatch.setattr(common, "_lock_path_for", lambda name: str(lock))
    monkeypatch.setattr(common, "send_telegram", lambda msg: None)

    # Write a PID that's very unlikely to exist (pid 999999 on Linux is not
    # normally allocated; we'll still guard by checking it's not alive first)
    dead_pid = 999999
    try:
        os.kill(dead_pid, 0)
        pytest.skip(f"PID {dead_pid} unexpectedly alive on this system")
    except ProcessLookupError:
        pass

    lock.write_text(str(dead_pid))
    common.acquire_lock("test")

    assert int(lock.read_text().strip()) == os.getpid()
    common.release_lock("test")


def test_release_lock_is_idempotent(tmp_path, monkeypatch):
    from cron import common

    lock = tmp_path / "tradebot_test.lock"
    monkeypatch.setattr(common, "_lock_path_for", lambda name: str(lock))

    # Release on a missing lock must not raise
    common.release_lock("test")
    assert not lock.exists()
