from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "untitled"


@dataclass
class Project:
    name: str
    root: Path

    _DIRS = ("source", "audio", "transcript", "ideas", "voice", "renders")

    @property
    def source_dir(self) -> Path:
        return self.root / "source"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def transcript_dir(self) -> Path:
        return self.root / "transcript"

    @property
    def ideas_dir(self) -> Path:
        return self.root / "ideas"

    @property
    def voice_dir(self) -> Path:
        return self.root / "voice"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def video_path(self) -> Path:
        return self.source_dir / "video.mp4"

    @property
    def info_json_path(self) -> Path:
        return self.source_dir / "video.info.json"

    @property
    def audio_path(self) -> Path:
        return self.audio_dir / "source.mp3"

    @property
    def transcript_json_path(self) -> Path:
        return self.transcript_dir / "transcript.json"

    @property
    def transcript_txt_path(self) -> Path:
        return self.transcript_dir / "transcript.txt"

    @property
    def prompt_path(self) -> Path:
        return self.ideas_dir / "prompt.json"

    def idea_file(self, slug: str) -> Path:
        return self.ideas_dir / f"{slug}.md"

    def voice_file(self, slug: str) -> Path:
        return self.voice_dir / f"{slug}.mp3"

    def render_file(self, slug: str) -> Path:
        return self.renders_dir / f"{slug}.mp4"

    def plan_file(self, slug: str) -> Path:
        return self.renders_dir / f"{slug}.plan.json"

    def ensure_dirs(self) -> None:
        for name in self._DIRS:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(cls, projects_dir: Path, name: str) -> "Project":
        root = projects_dir / name
        if root.exists():
            raise FileExistsError(f"project already exists: {root}")
        project = cls(name=name, root=root)
        project.ensure_dirs()
        return project

    @classmethod
    def load(cls, projects_dir: Path, name: str) -> "Project":
        root = projects_dir / name
        if not root.is_dir():
            raise FileNotFoundError(f"no such project: {name} ({root})")
        return cls(name=name, root=root)

    @classmethod
    def list_all(cls, projects_dir: Path) -> list["Project"]:
        if not projects_dir.is_dir():
            return []
        out: list[Project] = []
        for child in sorted(projects_dir.iterdir()):
            if child.is_dir() and (child / "manifest.json").is_file():
                out.append(cls(name=child.name, root=child))
        return out

    @classmethod
    def discover(cls, projects_dir: Path, name: str | None) -> "Project":
        if name:
            return cls.load(projects_dir, name)
        projects = cls.list_all(projects_dir)
        if not projects:
            raise FileNotFoundError(
                "no projects found; run `python -m shorts fetch <url>` first"
            )
        if len(projects) == 1:
            return projects[0]
        return max(projects, key=lambda p: p.manifest_path.stat().st_mtime)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utcnow_iso() -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


@dataclass
class Manifest:
    name: str
    source: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)
    ideas: dict = field(default_factory=dict)
    publish: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)

    @classmethod
    def new(cls, name: str) -> "Manifest":
        return cls(name=name)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            source=data.get("source", {}),
            stages=data.get("stages", {}),
            ideas=data.get("ideas", {}),
            publish=data.get("publish", {}),
            settings=data.get("settings", {}),
        )

    def _to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "stages": self.stages,
            "ideas": self.ideas,
            "publish": self.publish,
            "settings": self.settings,
        }

    def save(self, path: Path) -> None:
        path = Path(path)
        # unique per save: concurrent saves (the UI fetches the YouTube and TikTok
        # queues at once, and each one saves) must not share a temp file, or one
        # os.replace finds its source already moved by the other
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(
                json.dumps(self._to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def stage_done(self, stage: str, **fields) -> None:
        entry = self.stages.get(stage, {})
        entry["status"] = "done"
        entry["at"] = utcnow_iso()
        entry.update(fields)
        self.stages[stage] = entry

    def stage_skipped(self, stage: str, **fields) -> None:
        entry = self.stages.get(stage, {})
        entry["status"] = "skipped"
        entry["at"] = utcnow_iso()
        entry.update(fields)
        self.stages[stage] = entry

    def get_stage(self, stage: str) -> dict | None:
        return self.stages.get(stage)

    def is_stage_done(self, stage: str) -> bool:
        return self.stages.get(stage, {}).get("status") == "done"

    def is_stage_skipped(self, stage: str) -> bool:
        return (self.stages.get(stage) or {}).get("status") == "skipped"

    def set_idea(self, slug: str, **fields) -> None:
        entry = self.ideas.get(slug, {})
        entry.update(fields)
        self.ideas[slug] = entry

    def get_idea(self, slug: str) -> dict:
        return self.ideas.get(slug, {})

    def set_publish(self, **fields) -> None:
        self.publish.update(fields)

    def get_publish(self) -> dict:
        return self.publish

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

