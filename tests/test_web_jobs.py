import sys
import time

import pytest

from shorts.web.jobs import (
    JobBusy, JobRunner, sse_format, stage_argv,
)


def test_stage_argv_variants():
    assert stage_argv("ideate", "demo") == ["ideate", "demo"]
    assert stage_argv("ideate", "demo", count=4) == ["ideate", "demo", "--count", "4"]
    assert stage_argv("voice", "demo", force=True) == ["voice", "demo", "--force"]
    assert stage_argv("fetch", "demo", url="http://x") == [
        "fetch", "http://x", "--name", "demo",
    ]
    assert stage_argv("fetch", "demo", url="http://x", count=5) == [
        "fetch", "http://x", "--name", "demo", "--count", "5",
    ]
    with pytest.raises(ValueError):
        stage_argv("fetch", "demo")           # no url
    with pytest.raises(ValueError):
        stage_argv("bogus", "demo")


def test_sse_format():
    assert sse_format({"type": "line", "text": "hi"}) == 'data: {"type": "line", "text": "hi"}\n\n'


def _drain(runner, timeout=5.0):
    deadline = time.time() + timeout
    while runner.running() and time.time() < deadline:
        time.sleep(0.02)


def test_runner_captures_output_and_exit_code(tmp_path):
    runner = JobRunner(tmp_path)
    runner.start("x", "demo", [sys.executable, "-c", "print('hello'); print('world')"])
    _drain(runner)
    st = runner.state()
    assert st["returncode"] == 0
    assert st["running"] is False
    buffered = [e for e in _collect(runner) if e["type"] == "line"]
    assert any(e["text"] == "hello" for e in buffered)
    assert any(e["text"] == "world" for e in buffered)


def test_attach_replays_exit_for_a_finished_job(tmp_path):
    runner = JobRunner(tmp_path)
    runner.start("x", "demo", [sys.executable, "-c", "print('done')"])
    _drain(runner)
    events = _collect(runner)  # attach() AFTER the live exited broadcast is gone
    statuses = [e for e in events if e["type"] == "status"]
    assert statuses == [{"type": "status", "state": "exited", "returncode": 0,
                         "finished_at": runner.state()["finished_at"]}]
    assert not any(e["state"] == "idle" for e in statuses)


def test_attach_sends_idle_when_no_job_ran(tmp_path):
    events = _collect(JobRunner(tmp_path))
    assert events == [{"type": "status", "state": "idle"}]


def _collect(runner):
    q = runner.attach()
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    runner.detach(q)
    return out


def test_runner_rejects_second_job(tmp_path):
    runner = JobRunner(tmp_path)
    runner.start("x", "demo", [sys.executable, "-c", "import time; time.sleep(2)"])
    with pytest.raises(JobBusy):
        runner.start("y", "demo", [sys.executable, "-c", "pass"])
    runner.cancel()
    _drain(runner)


def test_runner_cancel_terminates(tmp_path):
    runner = JobRunner(tmp_path)
    runner.start("x", "demo", [sys.executable, "-c", "import time; time.sleep(30)"])
    assert runner.running() is True
    runner.cancel()
    _drain(runner)
    assert runner.running() is False
    assert runner.state()["returncode"] != 0


def test_cancel_with_nothing_running_raises(tmp_path):
    with pytest.raises(JobBusy):
        JobRunner(tmp_path).cancel()


def test_stage_argv_publish():
    from shorts.web.jobs import stage_argv
    assert stage_argv("publish", "demo") == ["publish", "demo", "--platform", "youtube"]
    assert stage_argv("publish", "demo", slugs=["01-x", "02-y"], force=True) == [
        "publish", "demo", "--platform", "youtube", "--slug", "01-x", "--slug", "02-y", "--force",
    ]


def test_stage_argv_publish_tiktok():
    from shorts.web.jobs import stage_argv
    assert stage_argv("publish", "demo", platform="tiktok") == ["publish", "demo", "--platform", "tiktok"]


def test_stage_argv_youtube_auth():
    from shorts.web.jobs import stage_argv
    assert stage_argv("youtube-auth", "") == ["youtube", "auth"]


def test_stage_argv_tiktok_auth():
    from shorts.web.jobs import stage_argv
    assert stage_argv("tiktok-auth", "") == ["tiktok", "auth"]


def test_stage_argv_unknown_auth_stage_raises():
    from shorts.web.jobs import stage_argv
    with pytest.raises(ValueError):
        stage_argv("bogus-auth", "")
