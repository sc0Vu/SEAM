"""The state dir's single-writer lock (store/lock.py)."""

import subprocess
import sys
import time

import pytest

from store.lock import StoreBusy, active_writer, writer_lock


def test_second_writer_is_refused_and_told_who_holds_it(tmp_path):
    with writer_lock(tmp_path, "run"):
        assert active_writer(tmp_path).startswith("run, pid ")
        with pytest.raises(StoreBusy, match="run, pid"):
            with writer_lock(tmp_path, "cancel"):
                pass
    assert active_writer(tmp_path) is None
    with writer_lock(tmp_path, "cancel"):  # free again once released
        pass


def test_no_lock_file_means_no_writer(tmp_path):
    assert active_writer(tmp_path / "fresh") is None


def test_lock_dies_with_its_holder(tmp_path):
    """A killed `run` (crash, kill -9) leaves no stale lock behind."""
    code = ("import sys, time; sys.path.insert(0, sys.argv[2]); "
            "from store.lock import writer_lock\n"
            "with writer_lock(sys.argv[1], 'run'):\n"
            "    print('held', flush=True); time.sleep(60)")
    root = str(__import__("pathlib").Path(__file__).parent.parent)
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), root],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        assert active_writer(tmp_path) is not None
        proc.kill()
        proc.wait(10)
        deadline = time.time() + 5
        while active_writer(tmp_path) is not None and time.time() < deadline:
            time.sleep(0.05)
        assert active_writer(tmp_path) is None
    finally:
        proc.kill()
