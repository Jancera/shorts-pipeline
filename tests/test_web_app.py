import io
import json
import os
from pathlib import Path

import pytest

from shorts.config import (
    Config, IdeateCfg, RenderCfg, SubtitleCfg, TikTokCfg, TranscribeCfg, VoiceCfg,
    YouTubeCfg, load_config,
)
from shorts.project import Manifest, Project
from shorts.web.app import create_app


def _config(tmp_path: Path) -> Config:
    (tmp_path / "assets").mkdir()
    return Config(
        root=tmp_path,
        projects_dir=tmp_path / "projects",
        assets_dir=tmp_path / "assets",
        aspect="9:16",
        transcribe=TranscribeCfg(model="whisper-1"),
        ideate=IdeateCfg(model="gpt-4.1", desired_video_length=90),
        voice=VoiceCfg(
            base_url="http://localhost:3900", model="m", voice="v",
            language=None, speed=1.0, instruct=None,
        ),
        render=RenderCfg(
            min_beat_duration=3.0,
            video_fit_mode="cover",
            subtitle=SubtitleCfg(
                enabled=True, font=None, font_size=None, primary_color=None,
                bold=False, italic=False, uppercase=False, position=None,
                margin_vertical=None, max_chars_per_line=None, max_lines=None,
                max_duration=None,
            ),
        ),
        openai_api_key="sk-test",
        youtube=YouTubeCfg(client_secret=None,
                           token_path=tmp_path / ".youtube_token.json",
                           category_id=22),
        tiktok=TikTokCfg(client_key=None, client_secret=None,
                         token_path=tmp_path / ".tiktok_token.json",
                         privacy_level="SELF_ONLY",
                         disable_duet=False, disable_stitch=False,
                         disable_comment=False, is_aigc=False),
    )


def _idea_md(slug, narration, approved=False):
    box = "x" if approved else " "
    return (
        f"---\nslug: {slug}\ntitle: Demo Idea\n---\n\n"
        f"- [{box}] Approved\n\n## Description\n\nold caption\n\n"
        f"## Tags\n\nold, tags\n\n## Hook\n\nh\n\n"
        f"## Narration\n\n{narration}\n\n## Notes\n\nn\n"
    )


@pytest.fixture
def client(tmp_path):
    cfg = _config(tmp_path)
    project = Project.create(cfg.projects_dir, "demo")
    Manifest.new("demo").save(project.manifest_path)
    project.idea_file("01-x").write_text(_idea_md("01-x", "the script", approved=True))
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app.test_client(), cfg, project


def test_get_projects_lists_demo(client):
    c, _, _ = client
    rows = c.get("/api/projects").get_json()
    assert [r["name"] for r in rows] == ["demo"]
    assert "fetch" in rows[0]["stages"]


def test_get_project_snapshot(client):
    c, _, _ = client
    snap = c.get("/api/projects/demo").get_json()
    assert snap["name"] == "demo"
    assert [s["stage"] for s in snap["stages"]] == [
        "fetch", "transcribe", "ideate", "voice", "plan", "render",
    ]
    assert snap["ideas"][0]["slug"] == "01-x"
    assert snap["ideas"][0]["approved"] is True
    assert snap["job"] is None
    assert "settings" in snap
    assert snap["settings"] == {}


def test_get_unknown_project_404(client):
    c, _, _ = client
    resp = c.get("/api/projects/nope")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_corrupt_manifest_is_422_and_does_not_500_the_list(client):
    c, cfg, _ = client
    broken = cfg.projects_dir / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{ truncated")

    detail = c.get("/api/projects/broken")
    assert detail.status_code == 422
    assert "manifest.json" in detail.get_json()["error"]

    listing = c.get("/api/projects")
    assert listing.status_code == 200
    names = [r["name"] for r in listing.get_json()]
    assert "demo" in names and "broken" not in names


def test_get_jobs_current_idle(client):
    c, _, _ = client
    assert c.get("/api/jobs/current").get_json()["running"] is False


def test_index_served(client):
    c, _, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert b"EventSource" in resp.data
    assert b'<dialog id="refine-modal">' in resp.data
    assert b"refine-btn" in resp.data
    assert b"refine-prompt-input" in resp.data
    assert b"/refine" in resp.data


def test_index_serves_subtitle_settings_ui(client):
    c, _, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    # Settings gear button
    assert b"open-settings-btn" in resp.data
    assert b"settings-btn" in resp.data

    # Dialog modal
    assert b'<dialog id="subtitles-modal">' in resp.data
    assert b'<form id="subtitles-form"' in resp.data

    # 12 SubtitleCfg fields
    assert b'name="enabled"' in resp.data
    assert b'name="font"' in resp.data
    assert b'name="font_size"' in resp.data
    assert b'name="primary_color"' in resp.data
    assert b'name="bold"' in resp.data
    assert b'name="italic"' in resp.data
    assert b'name="uppercase"' in resp.data
    assert b'name="position"' in resp.data
    assert b'name="margin_vertical"' in resp.data
    assert b'name="max_chars_per_line"' in resp.data
    assert b'name="max_lines"' in resp.data
    assert b'name="max_duration"' in resp.data

    # Input types: color, number, select
    assert b'type="color"' in resp.data
    assert b'type="number"' in resp.data
    assert b'<select id="sub-position"' in resp.data

    # API endpoints called in JS
    assert b"/api/config/subtitles" in resp.data


def test_put_prompt_writes_file(client):
    c, _, project = client
    resp = c.put("/api/projects/demo/prompt", json={"prompt": "novo brief pt-br"})
    assert resp.status_code == 200
    assert json.loads(project.prompt_path.read_text()) == {"prompt": "novo brief pt-br"}
    assert resp.get_json()["prompt"] == "novo brief pt-br"


def test_put_prompt_empty_422(client):
    c, _, _ = client
    resp = c.put("/api/projects/demo/prompt", json={"prompt": "  "})
    assert resp.status_code == 422


def test_put_idea_updates_md(client):
    c, _, project = client
    resp = c.put(
        "/api/projects/demo/ideas/01-x",
        json={
            "title": "Renamed short",
            "description": "A punchier caption.",
            "tags": "gta 6, rockstar, leonida",
            "narration": "rewritten narration",
            "approved": False,
        },
    )
    assert resp.status_code == 200
    md = project.idea_file("01-x").read_text()
    assert "title: Renamed short\n" in md
    assert "## Description\n\nA punchier caption.\n" in md
    assert "## Tags\n\ngta 6, rockstar, leonida\n" in md
    assert "rewritten narration" in md
    assert "- [ ] Approved" in md
    assert "## Notes\n\nn" in md
    idea = resp.get_json()["ideas"][0]
    assert idea["title"] == "Renamed short"
    assert idea["description"] == "A punchier caption."
    assert idea["tags"] == "gta 6, rockstar, leonida"


def test_put_approved_toggles_only_the_checkbox(client):
    c, _, project = client
    before = project.idea_file("01-x").read_text()
    assert "- [x] Approved" in before
    resp = c.put("/api/projects/demo/ideas/01-x/approved", json={"approved": False})
    assert resp.status_code == 200
    md = project.idea_file("01-x").read_text()
    assert "- [ ] Approved" in md
    assert "the script" in md  # narration untouched
    assert "title: Demo Idea" in md
    assert resp.get_json()["ideas"][0]["approved"] is False


def test_put_approved_unknown_idea_404(client):
    c, _, _ = client
    assert c.put("/api/projects/demo/ideas/99-nope/approved",
                 json={"approved": True}).status_code == 404


def test_voice_route_serves_and_404s(client):
    c, _, project = client
    assert c.get("/api/projects/demo/voice/01-x").status_code == 404
    project.voice_file("01-x").write_bytes(b"ID3fake-mp3-bytes")
    resp = c.get("/api/projects/demo/voice/01-x")
    assert resp.status_code == 200
    assert resp.mimetype == "audio/mpeg"
    assert resp.data == b"ID3fake-mp3-bytes"


def test_categories_route(client):
    c, cfg, project = client
    (cfg.assets_dir / "neon").mkdir()
    (cfg.assets_dir / "neon" / "a.mp4").write_bytes(b"x")
    (cfg.assets_dir / "neon" / "b.mp4").write_bytes(b"x")
    project.plan_file("01-x").write_text(json.dumps({
        "beats": [{"category": "neon", "description": "neon skyline"},
                  {"category": "swamp", "description": "misty swamp"}]
    }))
    rows = c.get("/api/projects/demo/categories").get_json()
    by = {r["category"]: r for r in rows}
    assert by["neon"]["assets"] == 2
    assert by["neon"]["beats"] == ["neon skyline"]
    assert by["swamp"]["assets"] == 0  # referenced by a beat, no folder
    # categories a plan needs come before folder-only ones
    assert [r["category"] for r in rows][:2] == ["neon", "swamp"]


def test_put_idea_unknown_slug_404(client):
    c, _, _ = client
    resp = c.put(
        "/api/projects/demo/ideas/99-nope",
        json={"narration": "x", "approved": False},
    )
    assert resp.status_code == 404


def test_put_plan_valid_and_invalid(client):
    c, _, project = client
    ok = c.put(
        "/api/projects/demo/ideas/01-x/plan",
        json={"plan": '{"audio_path":"a.mp3","beats":[{"start":0,"end":1}]}'},
    )
    assert ok.status_code == 200
    assert json.loads(project.plan_file("01-x").read_text())["beats"][0]["end"] == 1

    bad = c.put("/api/projects/demo/ideas/01-x/plan", json={"plan": "{bad"})
    assert bad.status_code == 422


class _FakeRunner:
    def __init__(self):
        self.started = []
        self.busy = False
        self._q = None

    def running(self):
        return self.busy

    def state(self):
        return {"running": self.busy, "stage": "fetch" if self.busy else None,
                "project": "demo" if self.busy else None,
                "started_at": None, "returncode": None, "finished_at": None}

    def start(self, stage, project, cmd):
        from shorts.web.jobs import JobBusy
        if self.busy:
            raise JobBusy("busy")
        self.started.append((stage, project, cmd))
        self.busy = True

    def cancel(self):
        from shorts.web.jobs import JobBusy
        if not self.busy:
            raise JobBusy("idle")
        self.busy = False

    def attach(self):
        import queue
        q = queue.Queue()
        q.put({"type": "line", "text": "hello from stream"})
        q.put(None)
        return q

    def detach(self, q):
        pass


@pytest.fixture
def fake_client(tmp_path):
    cfg = _config(tmp_path)
    Project.create(cfg.projects_dir, "demo")
    Manifest.new("demo").save((cfg.projects_dir / "demo" / "manifest.json"))
    app = create_app(cfg)
    fake = _FakeRunner()
    app.config["JOB_RUNNER"] = fake
    return app.test_client(), fake


def test_post_run_stage_builds_argv(fake_client):
    c, fake = fake_client
    resp = c.post("/api/projects/demo/run/ideate", json={"force": True})
    assert resp.status_code == 202
    stage, project, cmd = fake.started[0]
    assert stage == "ideate" and project == "demo"
    assert cmd[-3:] == ["ideate", "demo", "--force"]
    assert cmd[1:3] == ["-m", "shorts"]


def test_post_run_stage_ideate_with_count(fake_client):
    c, fake = fake_client
    resp = c.post("/api/projects/demo/run/ideate", json={"count": 7})
    assert resp.status_code == 202
    stage, project, cmd = fake.started[0]
    assert stage == "ideate" and project == "demo"
    assert cmd[-4:] == ["ideate", "demo", "--count", "7"]


@pytest.mark.parametrize("bad_count", ["bad", 0, -1])
def test_post_run_stage_ideate_invalid_count_returns_422(client, bad_count):
    c, _, _ = client
    resp = c.post("/api/projects/demo/run/ideate", json={"count": bad_count})
    assert resp.status_code == 422
    assert "count must be an integer >= 1" in resp.get_json()["error"]


def test_post_run_bad_stage_400(fake_client):
    c, _ = fake_client
    assert c.post("/api/projects/demo/run/fetch").status_code == 400
    assert c.post("/api/projects/demo/run/bogus").status_code == 400


def test_post_run_busy_409(fake_client):
    c, fake = fake_client
    fake.busy = True
    assert c.post("/api/projects/demo/run/voice").status_code == 409


def test_post_projects_requires_url_and_name(fake_client):
    c, _ = fake_client
    assert c.post("/api/projects", json={"url": "http://x"}).status_code == 400
    assert c.post("/api/projects", json={"name": "y"}).status_code == 400


def test_post_projects_starts_fetch(fake_client):
    c, fake = fake_client
    resp = c.post("/api/projects", json={"url": "http://x", "name": "New Clip"})
    assert resp.status_code == 202
    stage, project, cmd = fake.started[0]
    assert stage == "fetch" and project == "new-clip"
    assert cmd[-4:] == ["fetch", "http://x", "--name", "new-clip"]


def test_post_projects_starts_fetch_with_count(fake_client):
    c, fake = fake_client
    resp = c.post("/api/projects", json={"url": "http://x", "name": "Count Clip", "count": 6})
    assert resp.status_code == 202
    stage, project, cmd = fake.started[0]
    assert stage == "fetch" and project == "count-clip"
    assert cmd[-6:] == ["fetch", "http://x", "--name", "count-clip", "--count", "6"]


def test_post_projects_text_payload(client):
    c, cfg, _ = client
    resp = c.post(
        "/api/projects",
        json={"name": "Text Project", "text": "Some instruction text"},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["source"]["type"] == "text"
    assert data["source"]["title"] == "Text Project"
    stages = {s["stage"]: s["status"] for s in data["stages"]}
    assert stages["fetch"] == "skipped"
    assert stages["transcribe"] == "skipped"
    assert stages["ideate"] == "ready"

    # Disk verification
    project_dir = cfg.projects_dir / "text-project"
    transcript_file = project_dir / "transcript" / "transcript.txt"
    assert transcript_file.read_text(encoding="utf-8") == "Some instruction text"


def test_post_projects_text_payload_with_count(client):
    c, cfg, _ = client
    resp = c.post(
        "/api/projects",
        json={"name": "Text Project Count", "text": "Some instruction text", "count": 8},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["settings"] == {"count": 8}
    m = Manifest.load(cfg.projects_dir / "text-project-count" / "manifest.json")
    assert m.settings == {"count": 8}
    assert m.get_setting("count") == 8


def test_post_projects_file_upload(client):
    c, cfg, _ = client
    resp = c.post(
        "/api/projects",
        data={
            "name": "File Project",
            "file": (io.BytesIO(b"hello markdown"), "notes.md"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["source"]["type"] == "file"
    assert data["source"]["title"] == "File Project"
    assert data["source"]["file"] == "notes.md"
    stages = {s["stage"]: s["status"] for s in data["stages"]}
    assert stages["fetch"] == "skipped"
    assert stages["transcribe"] == "skipped"
    assert stages["ideate"] == "ready"

    # Disk verification
    project_dir = cfg.projects_dir / "file-project"
    transcript_file = project_dir / "transcript" / "transcript.txt"
    assert transcript_file.read_text(encoding="utf-8") == "hello markdown"


def test_post_projects_file_upload_with_count(client):
    c, cfg, _ = client
    resp = c.post(
        "/api/projects",
        data={
            "name": "File Project Count",
            "file": (io.BytesIO(b"hello markdown"), "notes.md"),
            "count": 5,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["settings"] == {"count": 5}
    m = Manifest.load(cfg.projects_dir / "file-project-count" / "manifest.json")
    assert m.settings == {"count": 5}
    assert m.get_setting("count") == 5


@pytest.mark.parametrize("bad_count", ["bad", 0, -1])
def test_post_projects_invalid_count_returns_422(client, bad_count):
    c, _, _ = client
    resp = c.post("/api/projects", json={"name": "Bad Count", "text": "abc", "count": bad_count})
    assert resp.status_code == 422
    assert "count must be an integer >= 1" in resp.get_json()["error"]


def test_post_projects_file_unsupported_extension(client):
    c, _, _ = client
    resp = c.post(
        "/api/projects",
        data={
            "name": "Pdf Project",
            "file": (io.BytesIO(b"%PDF-1.4..."), "notes.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "only .txt and .md files are supported"


def test_post_projects_file_exceeds_5mb(client):
    c, _, _ = client
    big_data = b"x" * (5 * 1024 * 1024 + 1)
    resp = c.post(
        "/api/projects",
        data={
            "name": "Big Project",
            "file": (io.BytesIO(big_data), "big.txt"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "file exceeds 5MB limit"


def test_post_projects_neither_url_text_nor_file(client):
    c, _, _ = client
    resp = c.post("/api/projects", json={"name": "No Source"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_projects_multiple_sources_rejected(client):
    c, _, _ = client
    resp = c.post(
        "/api/projects",
        json={"name": "Both", "url": "http://x", "text": "y"},
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_projects_file_invalid_utf8(client):
    c, _, _ = client
    resp = c.post(
        "/api/projects",
        data={
            "name": "Invalid UTF8",
            "file": (io.BytesIO(b"\xff\xfe\x00\x00"), "invalid.txt"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_projects_existing_project_conflict_and_force(client):
    c, _, _ = client
    resp = c.post("/api/projects", json={"name": "demo", "text": "duplicate"})
    assert resp.status_code == 409
    assert "already exists" in resp.get_json()["error"]

    resp_force = c.post("/api/projects", json={"name": "demo", "text": "overwritten", "force": True})
    assert resp_force.status_code == 201


def test_stream_returns_buffered_event(fake_client):
    c, _ = fake_client
    resp = c.get("/api/jobs/current/stream")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    assert 'data: {"type": "line", "text": "hello from stream"}' in body


def test_cancel_idle_409(fake_client):
    c, _ = fake_client
    assert c.post("/api/jobs/current/cancel").status_code == 409


def test_youtube_status_not_configured(client):
    c, _, _ = client
    body = c.get("/api/youtube/status").get_json()
    assert body == {"connected": False, "channel": None, "error": "not configured"}


def test_youtube_status_connected(client, monkeypatch):
    c, cfg, _ = client
    (cfg.youtube.token_path).write_text("{}")  # file exists → not "not configured"
    import shorts.youtube as yt
    monkeypatch.setattr(yt, "get_credentials", lambda c: object())
    monkeypatch.setattr(yt, "channel_title", lambda c: "My Channel")
    body = c.get("/api/youtube/status").get_json()
    assert body == {"connected": True, "channel": "My Channel", "error": None}


def test_youtube_status_expired(client, monkeypatch):
    c, cfg, _ = client
    (cfg.youtube.token_path).write_text("{}")
    import shorts.youtube as yt
    monkeypatch.setattr(yt, "get_credentials",
        lambda c: (_ for _ in ()).throw(yt.YouTubeAuthError("x", reason="expired")))
    assert c.get("/api/youtube/status").get_json()["error"] == "token expired"


def test_tiktok_status_not_configured(client):
    c, _, _ = client
    body = c.get("/api/tiktok/status").get_json()
    assert body == {"connected": False, "channel": None, "error": "not configured"}


def _tiktok_configured_client(tmp_path):
    import dataclasses
    cfg = _config(tmp_path)
    cfg = dataclasses.replace(
        cfg, tiktok=dataclasses.replace(cfg.tiktok, client_key="ck", client_secret="cs")
    )
    project = Project.create(cfg.projects_dir, "demo")
    Manifest.new("demo").save(project.manifest_path)
    project.idea_file("01-x").write_text(_idea_md("01-x", "the script", approved=True))
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app.test_client(), cfg, project


def test_tiktok_status_connected(tmp_path, monkeypatch):
    c, _cfg, _ = _tiktok_configured_client(tmp_path)
    import shorts.tiktok as tt
    monkeypatch.setattr(tt, "get_credentials", lambda c: object())
    monkeypatch.setattr(tt, "account_label", lambda c: "my_open_id")
    body = c.get("/api/tiktok/status").get_json()
    assert body == {"connected": True, "channel": "my_open_id", "error": None}


def test_tiktok_status_expired(tmp_path, monkeypatch):
    c, _cfg, _ = _tiktok_configured_client(tmp_path)
    import shorts.tiktok as tt
    monkeypatch.setattr(tt, "get_credentials",
        lambda c: (_ for _ in ()).throw(tt.TikTokAuthError("x", reason="expired")))
    assert c.get("/api/tiktok/status").get_json()["error"] == "token expired"


def test_unknown_platform_status_404(client):
    c, _, _ = client
    assert c.get("/api/bogus/status").status_code == 404


def test_unknown_platform_auth_404(client):
    c, _, _ = client
    assert c.post("/api/bogus/auth").status_code == 404


def test_post_tiktok_auth_builds_argv(fake_client):
    c, fake = fake_client
    assert c.post("/api/tiktok/auth").status_code == 202
    stage, _project, cmd = fake.started[-1]
    assert stage == "tiktok-auth" and cmd[-2:] == ["tiktok", "auth"]


def test_publish_queue_platform_query_param(client):
    c, _, _ = client
    resp = c.get("/api/projects/demo/publish?platform=tiktok")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items"][0]["slug"] == "01-x"

    bad = c.get("/api/projects/demo/publish?platform=bogus")
    assert bad.status_code == 400


def test_put_cadence_and_queue(client):
    c, _, project = client
    r = c.put("/api/projects/demo/publish/cadence",
              json={"start": "2026-09-14T09:00:00Z", "interval_hours": 24, "weekdays": [1, 2, 3, 4, 5]})
    assert r.status_code == 200
    from shorts.project import Manifest
    assert Manifest.load(project.manifest_path).get_publish()["interval_hours"] == 24
    assert r.get_json()["cadence"]["weekdays"] == [1, 2, 3, 4, 5]

    r2 = c.put("/api/projects/demo/publish/cadence", json={"start": None})
    assert Manifest.load(project.manifest_path).get_publish() == {}


def test_put_cadence_bad_start_422(client):
    c, _, _ = client
    assert c.put("/api/projects/demo/publish/cadence",
                 json={"start": "not-a-date"}).status_code == 422


def test_put_cadence_weekdays_scalar_is_422_not_500(client):
    c, _, _ = client
    r = c.put("/api/projects/demo/publish/cadence",
              json={"start": "2026-09-14T09:00:00Z", "weekdays": 5})
    assert r.status_code == 422


def test_put_cadence_weekdays_bool_is_422(client):
    c, _, _ = client
    r = c.put("/api/projects/demo/publish/cadence",
              json={"start": "2026-09-14T09:00:00Z", "weekdays": [True]})
    assert r.status_code == 422


def test_put_cadence_with_daily_times(client):
    c, _, project = client
    r = c.put(
        "/api/projects/demo/publish/cadence",
        json={
            "start": "2026-09-20T10:00:00Z",
            "times": ["10:00", "14:00", "19:00"],
            "weekdays": [1, 2, 3],
        },
    )
    assert r.status_code == 200
    from shorts.project import Manifest
    assert Manifest.load(project.manifest_path).get_publish()["times"] == ["10:00", "14:00", "19:00"]
    body = r.get_json()
    assert body["cadence"]["times"] == ["10:00", "14:00", "19:00"]
    assert body["cadence"]["weekdays"] == [1, 2, 3]

    r2 = c.put(
        "/api/projects/demo/publish/cadence",
        json={"start": "2026-09-20T10:00:00Z", "interval_hours": 12},
    )
    assert r2.status_code == 200
    assert "times" not in Manifest.load(project.manifest_path).get_publish()
    assert "times" not in (r2.get_json()["cadence"] or {})


def test_put_cadence_invalid_times(client):
    c, _, _ = client
    start = "2026-09-20T10:00:00Z"
    for bad in ["10:00", 123, ["99:99"], ["invalid"], [123], ["10:00", "bad"]]:
        r = c.put("/api/projects/demo/publish/cadence", json={"start": start, "times": bad})
        assert r.status_code == 422
        assert r.get_json()["error"] == "times must be a list of HH:MM strings"


def test_put_publish_at(client):
    c, _, project = client
    r = c.put("/api/projects/demo/ideas/01-x/publish-at", json={"publish_at": "2026-10-01T12:00:00Z"})
    assert r.status_code == 200
    from shorts.project import Manifest
    assert Manifest.load(project.manifest_path).get_idea("01-x")["publish_at"] == "2026-10-01T12:00:00Z"
    c.put("/api/projects/demo/ideas/01-x/publish-at", json={"publish_at": None})
    assert "publish_at" not in Manifest.load(project.manifest_path).get_idea("01-x")


def test_post_publish_builds_argv(fake_client):
    c, fake = fake_client
    assert c.post("/api/projects/demo/publish").status_code == 202
    stage, project, cmd = fake.started[-1]
    assert stage == "publish" and cmd[-2:] == ["--platform", "youtube"] and cmd[1:3] == ["-m", "shorts"]
    fake.busy = False  # _FakeRunner does not auto-clear after a start
    c.post("/api/projects/demo/ideas/01-x/publish")
    _s, _p, cmd2 = fake.started[-1]
    assert cmd2[-6:] == ["publish", "demo", "--platform", "youtube", "--slug", "01-x"]


def test_post_youtube_auth_builds_argv(fake_client):
    c, fake = fake_client
    assert c.post("/api/youtube/auth").status_code == 202
    stage, _project, cmd = fake.started[-1]
    assert stage == "youtube-auth" and cmd[-2:] == ["youtube", "auth"]


def test_post_refine_title(client, monkeypatch):
    c, _, _ = client
    import shorts.web.app as web_app

    monkeypatch.setattr(web_app, "refine_idea_text", lambda *args, **kwargs: "Punchy New Title")
    resp = c.post("/api/projects/demo/ideas/01-x/refine", json={"field": "title"})
    assert resp.status_code == 200
    assert resp.get_json() == {"field": "title", "result": "Punchy New Title"}


def test_post_refine_description(client, monkeypatch):
    c, _, _ = client
    import shorts.web.app as web_app

    monkeypatch.setattr(web_app, "refine_idea_text", lambda *args, **kwargs: "Engaging new description.")
    resp = c.post("/api/projects/demo/ideas/01-x/refine", json={"field": "description"})
    assert resp.status_code == 200
    assert resp.get_json() == {"field": "description", "result": "Engaging new description."}


def test_post_refine_tags(client, monkeypatch):
    c, _, _ = client
    import shorts.web.app as web_app

    monkeypatch.setattr(web_app, "refine_idea_text", lambda *args, **kwargs: "tag1, tag2, tag3")
    resp = c.post("/api/projects/demo/ideas/01-x/refine", json={"field": "tags"})
    assert resp.status_code == 200
    assert resp.get_json() == {"field": "tags", "result": "tag1, tag2, tag3"}


def test_post_refine_with_prompt_and_current_values(client, monkeypatch):
    c, _, project = client
    manifest = Manifest.load(project.manifest_path)
    manifest.source = {"title": "Source Video"}
    manifest.save(project.manifest_path)

    import shorts.web.app as web_app
    recorded_kwargs = {}

    def mock_refine(*args, **kwargs):
        recorded_kwargs.update(kwargs)
        return "Refined Result"

    monkeypatch.setattr(web_app, "refine_idea_text", mock_refine)

    resp = c.post(
        "/api/projects/demo/ideas/01-x/refine",
        json={
            "field": "title",
            "prompt": "make it punchy",
            "current_title": "Custom Title",
            "current_description": "Custom Description",
            "current_tags": "tag1, tag2",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"field": "title", "result": "Refined Result"}
    assert recorded_kwargs["field"] == "title"
    assert recorded_kwargs["user_prompt"] == "make it punchy"
    assert recorded_kwargs["current_title"] == "Custom Title"
    assert recorded_kwargs["current_description"] == "Custom Description"
    assert recorded_kwargs["current_tags"] == "tag1, tag2"
    assert recorded_kwargs["narration"] == "the script"
    assert recorded_kwargs["hook"] == "h"
    assert recorded_kwargs["video_title"] == "Source Video"
    assert recorded_kwargs["model"] == "gpt-4.1"

    # Also verify fallback to file defaults when current values are omitted
    recorded_kwargs.clear()
    resp2 = c.post(
        "/api/projects/demo/ideas/01-x/refine",
        json={"field": "description"},
    )
    assert resp2.status_code == 200
    assert recorded_kwargs["field"] == "description"
    assert recorded_kwargs["user_prompt"] == ""
    assert recorded_kwargs["current_title"] == "Demo Idea"
    assert recorded_kwargs["current_description"] == "old caption"
    assert recorded_kwargs["current_tags"] == "old, tags"


def test_post_refine_invalid_field(client):
    c, _, _ = client
    # Missing field
    resp_empty = c.post("/api/projects/demo/ideas/01-x/refine", json={})
    assert resp_empty.status_code == 422
    assert "field must be one of: title, description, tags" in resp_empty.get_json()["error"]

    # Invalid field
    resp_invalid = c.post("/api/projects/demo/ideas/01-x/refine", json={"field": "hook"})
    assert resp_invalid.status_code == 422
    assert "field must be one of: title, description, tags" in resp_invalid.get_json()["error"]


def test_post_refine_not_found(client):
    c, _, _ = client
    # Nonexistent project
    resp_proj = c.post("/api/projects/nonexistent/ideas/01-x/refine", json={"field": "title"})
    assert resp_proj.status_code == 404
    assert "no such project: nonexistent" in resp_proj.get_json()["error"]

    # Nonexistent idea slug
    resp_idea = c.post("/api/projects/demo/ideas/nonexistent-idea/refine", json={"field": "title"})
    assert resp_idea.status_code == 404
    assert "no such idea: nonexistent-idea" in resp_idea.get_json()["error"]


def test_post_refine_openai_error(client, monkeypatch):
    c, cfg, _ = client
    import dataclasses
    import shorts.web.app as web_app

    # Missing API key (None and empty string)
    app_none = web_app.create_app(dataclasses.replace(cfg, openai_api_key=None))
    resp_none = app_none.test_client().post("/api/projects/demo/ideas/01-x/refine", json={"field": "title"})
    assert resp_none.status_code == 500
    assert "OPENAI_API_KEY is not configured" in resp_none.get_json()["error"]

    app_empty = web_app.create_app(dataclasses.replace(cfg, openai_api_key=""))
    resp_empty = app_empty.test_client().post("/api/projects/demo/ideas/01-x/refine", json={"field": "title"})
    assert resp_empty.status_code == 500
    assert "OPENAI_API_KEY is not configured" in resp_empty.get_json()["error"]

    # OpenAI / execution exception
    def mock_raise(*args, **kwargs):
        raise RuntimeError("OpenAI rate limit reached")

    monkeypatch.setattr(web_app, "refine_idea_text", mock_raise)
    resp_err = c.post("/api/projects/demo/ideas/01-x/refine", json={"field": "title"})
    assert resp_err.status_code == 500
    assert "OpenAI rate limit reached" in resp_err.get_json()["error"]


def _setup_subtitles_app(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    toml_content = (
        'projects_dir = "projects"\n'
        f'assets_dir = "{assets.as_posix()}"\n'
        'aspect = "9:16"\n\n'
        '[render]\n'
        'min_beat_duration = 3.0\n\n'
        '[render.subtitle]\n'
        'enabled = true\n'
    )
    (tmp_path / "config.toml").write_text(toml_content, encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app.test_client(), app, tmp_path


def test_get_subtitles_config_returns_current(client):
    c, _, _ = client
    resp = c.get("/api/config/subtitles")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "enabled": True,
        "font": None,
        "font_size": None,
        "primary_color": None,
        "bold": False,
        "italic": False,
        "uppercase": False,
        "position": None,
        "margin_vertical": None,
        "max_chars_per_line": None,
        "max_lines": None,
        "max_duration": None,
    }


def test_put_subtitles_config_success(tmp_path):
    c, app, root = _setup_subtitles_app(tmp_path)
    payload = {
        "enabled": True,
        "font": "Impact",
        "font_size": 24,
        "primary_color": "#FFCC00",
        "bold": True,
        "italic": True,
        "uppercase": True,
        "position": "middle",
        "margin_vertical": 40,
        "max_chars_per_line": 25,
        "max_lines": 3,
        "max_duration": 4.5,
    }
    resp = c.put("/api/config/subtitles", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["font"] == "Impact"
    assert data["font_size"] == 24
    assert data["primary_color"] == "#FFCC00"
    assert data["bold"] is True
    assert data["italic"] is True
    assert data["uppercase"] is True
    assert data["position"] == "middle"
    assert data["margin_vertical"] == 40
    assert data["max_chars_per_line"] == 25
    assert data["max_lines"] == 3
    assert data["max_duration"] == 4.5

    # Verify app.config was updated without server restart
    assert app.config["SHORTS_CONFIG"].render.subtitle.font == "Impact"
    assert app.config["SHORTS_CONFIG"].render.subtitle.font_size == 24
    assert app.config["SHORTS_CONFIG"].render.subtitle.primary_color == "#FFCC00"

    # Verify subsequent GET returns updated values
    get_resp = c.get("/api/config/subtitles")
    assert get_resp.status_code == 200
    assert get_resp.get_json() == data

    # Verify disk was updated
    content = (root / "config.toml").read_text(encoding="utf-8")
    assert 'font = "Impact"' in content
    assert 'font_size = 24' in content
    assert 'primary_color = "#FFCC00"' in content


def test_put_subtitles_config_aliases_and_reverts(tmp_path):
    c, app, _ = _setup_subtitles_app(tmp_path)
    resp = c.put("/api/config/subtitles", json={"size": 18, "color": "#00FF00"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["font_size"] == 18
    assert data["primary_color"] == "#00FF00"
    assert app.config["SHORTS_CONFIG"].render.subtitle.font_size == 18
    assert app.config["SHORTS_CONFIG"].render.subtitle.primary_color == "#00FF00"

    # Revert styling keys by passing empty dict
    resp2 = c.put("/api/config/subtitles", json={})
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert data2["font_size"] is None
    assert data2["primary_color"] is None
    assert app.config["SHORTS_CONFIG"].render.subtitle.font_size is None
    assert app.config["SHORTS_CONFIG"].render.subtitle.primary_color is None


def test_put_subtitles_config_toggle_enabled(tmp_path):
    c, app, _ = _setup_subtitles_app(tmp_path)
    resp = c.put("/api/config/subtitles", json={"enabled": False, "font": "Arial"})
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is False
    assert resp.get_json()["font"] == "Arial"
    assert app.config["SHORTS_CONFIG"].render.subtitle.enabled is False

    resp2 = c.put("/api/config/subtitles", json={"enabled": True})
    assert resp2.status_code == 200
    assert resp2.get_json()["enabled"] is True
    assert app.config["SHORTS_CONFIG"].render.subtitle.enabled is True


@pytest.mark.parametrize("payload,expected_err", [
    ({"position": "sideways"}, "render.subtitle.position must be one of"),
    ({"primary_color": "blue"}, "render.subtitle.primary_color must be a hex color"),
    ({"font_size": 0}, "render.subtitle.font_size must be >= 1"),
    ({"font_size": -1}, "render.subtitle.font_size must be >= 1"),
    ({"font_size": "abc"}, "render.subtitle.font_size must be an integer"),
    ({"margin_vertical": -1}, "render.subtitle.margin_vertical must be >= 0"),
    ({"max_chars_per_line": 0}, "render.subtitle.max_chars_per_line must be >= 1"),
    ({"max_lines": 0}, "render.subtitle.max_lines must be >= 1"),
    ({"max_duration": 0}, "render.subtitle.max_duration must be > 0"),
    ({"max_duration": -1.0}, "render.subtitle.max_duration must be > 0"),
    ({"max_duration": "not-a-number"}, "render.subtitle.max_duration must be a number"),
])
def test_put_subtitles_config_validation_errors(tmp_path, payload, expected_err):
    c, _, _ = _setup_subtitles_app(tmp_path)
    resp = c.put("/api/config/subtitles", json=payload)
    assert resp.status_code == 422
    assert expected_err in resp.get_json()["error"]


def test_put_subtitles_config_invalid_body_format(tmp_path):
    c, _, _ = _setup_subtitles_app(tmp_path)

    # Malformed / non-JSON data
    resp_bad_json = c.put("/api/config/subtitles", data="not json", content_type="application/json")
    assert resp_bad_json.status_code == 400
    assert "error" in resp_bad_json.get_json()

    # Empty body
    resp_empty = c.put("/api/config/subtitles", data="", content_type="application/json")
    assert resp_empty.status_code == 400
    assert "error" in resp_empty.get_json()

    # JSON array instead of object
    resp_list = c.put("/api/config/subtitles", json=["font", "Arial"])
    assert resp_list.status_code == 422
    assert "payload must be a dict" in resp_list.get_json()["error"]

    # JSON scalar instead of object
    resp_scalar = c.put("/api/config/subtitles", json="some string")
    assert resp_scalar.status_code == 422
    assert "payload must be a dict" in resp_scalar.get_json()["error"]


def test_atomic_write_preserves_permissions(tmp_path):
    from shorts.web.app import _atomic_write

    target = tmp_path / "target.txt"
    target.write_text("initial content", encoding="utf-8")
    os.chmod(target, 0o644)

    _atomic_write(target, "new content")

    assert target.read_text(encoding="utf-8") == "new content"
    assert os.stat(target).st_mode & 0o777 == 0o644


def test_atomic_write_cleans_up_on_failure(tmp_path, monkeypatch):
    from shorts.web.app import _atomic_write

    target = tmp_path / "target.txt"
    target.write_text("initial content", encoding="utf-8")

    def mock_replace(src, dst):
        raise OSError("Replace failed")

    monkeypatch.setattr(os, "replace", mock_replace)
    with pytest.raises(OSError, match="Replace failed"):
        _atomic_write(target, "updated content")

    assert target.read_text(encoding="utf-8") == "initial content"
    remaining = [p.name for p in tmp_path.iterdir()]
    assert remaining == ["target.txt"]



