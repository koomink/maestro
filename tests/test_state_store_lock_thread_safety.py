import threading
import time

from maestro.state.store import StateStore


def test_writer_lock_serializes_across_threads(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    concurrent = 0
    max_concurrent = 0
    counter_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        nonlocal concurrent, max_concurrent
        barrier.wait()
        with store.writer_lock("thread"):
            with counter_lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.05)
            with counter_lock:
                concurrent -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_concurrent == 1


def test_live_order_lock_serializes_across_threads(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    concurrent = 0
    max_concurrent = 0
    counter_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        nonlocal concurrent, max_concurrent
        barrier.wait()
        with store.live_order_lock("thread"):
            with counter_lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.05)
            with counter_lock:
                concurrent -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_concurrent == 1


def test_writer_lock_still_reentrant_within_same_thread(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    entered_nested = False

    with store.writer_lock("outer"):
        with store.writer_lock("inner"):
            entered_nested = True

    assert entered_nested is True
