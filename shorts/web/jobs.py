from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from shorts.project import utcnow_iso

ALLOWED_STAGES = ("fetch", "transcribe", "ideate", "voice", "plan", "render")
_RING = 2000
_HEARTBEAT_SECONDS = 15


class JobBusy(Exception):
    pass


def stage_argv(
    stage: str,
    name: str,
    *,
    url: str | None = None,
    force: bool = False,
    slugs: list[str] | None = None,
    platform: str = "youtube",
    count: int | None = None,
    desired_length: int | None = None,
) -> list[str]:
    if stage == "publish":
        argv = ["publish", name, "--platform", platform]
        for s in slugs or []:
            argv += ["--slug", s]
        if force:
            argv.append("--force")
        return argv
    if stage.endswith("-auth"):
        p = stage[: -len("-auth")]
        if p not in ("youtube", "tiktok"):
            raise ValueError(f"unknown stage: {stage}")
        return [p, "auth"]
    if stage not in ALLOWED_STAGES:
        raise ValueError(f"unknown stage: {stage}")
    if stage == "fetch":
        if not url:
            raise ValueError("fetch requires a url")
        argv = ["fetch", url, "--name", name]
        if count is not None:
            argv += ["--count", str(count)]
    else:
        argv = [stage, name]
        if stage == "ideate":
            if count is not None:
                argv += ["--count", str(count)]
            if desired_length is not None:
                argv += ["--desired-length", str(desired_length)]
        if stage == "voice" and slugs:
            for s in slugs:
                argv += ["--slug", s]
    if force:
        argv.append("--force")
    return argv


def sse_format(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@dataclass
class Job:
    id: str
    stage: str
    project: str
    cmd: list[str]
    started_at: str
    proc: subprocess.Popen | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=_RING))
    returncode: int | None = None
    finished_at: str | None = None
    canceled: bool = False


class JobRunner:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._listeners: list[queue.Queue] = []

    # ---- lifecycle -----------------------------------------------------

    def running(self) -> bool:
        job = self._job
        return bool(job and job.proc and job.returncode is None)

    def start(self, stage: str, project: str, cmd: list[str]) -> Job:
        with self._lock:
            if self.running():
                raise JobBusy(
                    f"a job is already running: {self._job.stage} on {self._job.project}"
                )
            job = Job(
                id=uuid.uuid4().hex,
                stage=stage,
                project=project,
                cmd=list(cmd),
                started_at=utcnow_iso(),
            )
            job.proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            self._job = job
        self._broadcast({
            "type": "status", "state": "running",
            "stage": job.stage, "project": job.project, "started_at": job.started_at,
        })
        threading.Thread(target=self._reader, args=(job,), daemon=True).start()
        return job

    def _reader(self, job: Job) -> None:
        assert job.proc and job.proc.stdout
        for line in job.proc.stdout:
            line = line.rstrip("\n")
            job.lines.append(line)
            self._broadcast({"type": "line", "text": line})
        job.proc.wait()
        job.returncode = job.proc.returncode
        job.finished_at = utcnow_iso()
        self._broadcast({
            "type": "status", "state": "exited",
            "returncode": job.returncode, "finished_at": job.finished_at,
        })

    def cancel(self) -> None:
        with self._lock:
            if not self.running():
                raise JobBusy("no job is running")
            job = self._job
            job.canceled = True
            try:
                pgid = os.getpgid(job.proc.pid)
            except ProcessLookupError:
                return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(30):
            if job.returncode is not None:
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # ---- listeners ---------------------------------------------------

    def attach(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            job = self._job
            finished = bool(job and job.returncode is not None)
            # Emit the status event FIRST so the frontend's "a newly-started job
            # clears the log" branch runs before the replayed buffer is appended
            # (otherwise a mid-job reload wipes the lines it just replayed).
            if self.running():
                q.put({
                    "type": "status", "state": "running",
                    "stage": job.stage, "project": job.project,
                    "started_at": job.started_at,
                })
            elif not finished:
                q.put({"type": "status", "state": "idle"})
            if job:
                for line in list(job.lines):
                    q.put({"type": "line", "text": line})
            if finished:
                # Replay the outcome so a listener that connected after the live
                # `exited` broadcast (e.g. across an SSE reconnect) still learns
                # the job ended — otherwise the UI stays stuck "running".
                q.put({
                    "type": "status", "state": "exited",
                    "returncode": job.returncode, "finished_at": job.finished_at,
                })
            self._listeners.append(q)
        return q

    def detach(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _broadcast(self, event: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            q.put(event)

    # ---- state -----------------------------------------------------

    def state(self) -> dict:
        job = self._job
        if not job:
            return {
                "running": False, "stage": None, "project": None,
                "started_at": None, "returncode": None, "finished_at": None,
            }
        return {
            "running": self.running(),
            "stage": job.stage,
            "project": job.project,
            "started_at": job.started_at,
            "returncode": job.returncode,
            "finished_at": job.finished_at,
        }
