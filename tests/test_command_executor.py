import threading

from motioncontrol.command_executor import CommandExecutor


def test_bounded_queue_preserves_order_and_owns_submitted_data():
    entered, release, finished = (threading.Event() for _ in range(3))
    seen = []

    def execute(action):
        seen.append(action["target"])
        if len(seen) == 1:
            entered.set()
            assert release.wait(2)
        if len(seen) == 3:
            finished.set()

    worker = CommandExecutor(execute, capacity=2)
    try:
        worker.submit({"target": "first"})
        assert entered.wait(2)
        action = {"target": "second"}
        assert worker.submit(action)["queued"]
        action["target"] = "modified"
        assert worker.submit({"target": "third"})["queued"]
        assert not worker.submit({"target": "overflow"})["queued"]
        release.set()
        assert finished.wait(2)
        assert seen == ["first", "second", "third"]
    finally:
        release.set()
        worker.close()


def test_invalidate_cancels_waiting_and_inflight_old_generation():
    entered, release, finished = (threading.Event() for _ in range(3))
    committed = []

    def execute(action):
        if action["target"] == "old":
            entered.set()
            assert release.wait(2)
        if worker.is_current(action["_command_generation"]):
            committed.append(action["target"])
        if action["target"] == "new":
            finished.set()

    worker = CommandExecutor(execute)
    try:
        worker.submit({"target": "old"})
        assert entered.wait(2)
        worker.submit({"target": "waiting"})
        worker.invalidate()
        worker.submit({"target": "new"})
        release.set()
        assert finished.wait(2)
        assert committed == ["new"]
    finally:
        release.set()
        worker.close()


def test_failed_command_does_not_stop_the_worker_and_close_rejects_submissions():
    done = threading.Event()

    def execute(action):
        if action["target"] == "fail":
            raise ValueError("failed command")
        done.set()

    worker = CommandExecutor(execute)
    worker.submit({"target": "fail"})
    worker.submit({"target": "next"})
    try:
        assert done.wait(2)
        assert worker.last_error == "failed command"
    finally:
        worker.close()
    assert not worker.submit({"target": "closed"})["queued"]
