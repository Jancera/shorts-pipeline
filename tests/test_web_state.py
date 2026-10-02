import json
from datetime import datetime, timedelta, timezone

from shorts.config import (
    Config, IdeateCfg, RenderCfg, SubtitleCfg, TikTokCfg, TranscribeCfg, VoiceCfg,
    YouTubeCfg,
)
from shorts.project import Manifest, Project, sha256_text
from shorts.publish import iso
from shorts.web.state import (
    build_snapshot, category_report, idea_freshness, list_projects,
    prompt_text, stage_rows,
)


def _config(tmp_path):
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
        f"---\nslug: {slug}\ntitle: T\n---\n\n"
        f"- [{box}] Approved\n\n## Description\n\ndesc for {slug}\n\n"
        f"## Tags\n\ntag one, tag two\n\n"
        f"## Hook\n\nh\n\n"
        f"## Narration\n\n{narration}\n\n## Notes\n\nn\n"
    )


def _project(tmp_path, name="demo"):
    cfg = _config(tmp_path)
    project = Project.create(cfg.projects_dir, name)
    Manifest.new(name).save(project.manifest_path)
    return cfg, project


def test_stage_rows_fresh_project_only_fetch_ready(tmp_path):
    cfg, project = _project(tmp_path)
    rows = stage_rows(project, Manifest.load(project.manifest_path), cfg)
    by = {r["stage"]: r for r in rows}
    assert [r["stage"] for r in rows] == [
        "fetch", "transcribe", "ideate", "voice", "plan", "render",
    ]
    assert by["fetch"]["status"] == "ready"
    assert by["transcribe"]["status"] == "blocked"
    assert by["render"]["status"] == "blocked"


def test_stage_rows_after_fetch_and_transcribe(tmp_path):
    cfg, project = _project(tmp_path)
    project.video_path.write_bytes(b"x")
    project.transcript_txt_path.write_text("hello")
    m = Manifest.load(project.manifest_path)
    m.stage_done("fetch")
    m.stage_done("transcribe", audio_sha256="none", outputs=[])
    m.save(project.manifest_path)
    by = {r["stage"]: r for r in stage_rows(project, m, cfg)}
    assert by["fetch"]["status"] == "done"
    assert by["transcribe"]["status"] == "done"
    assert by["ideate"]["status"] == "ready"


def test_stage_rows_when_fetch_and_transcribe_skipped(tmp_path):
    cfg, project = _project(tmp_path)
    project.transcript_txt_path.write_text("user idea text")
    m = Manifest.load(project.manifest_path)
    m.stage_skipped("fetch")
    m.stage_skipped("transcribe")
    m.save(project.manifest_path)
    rows = stage_rows(project, m, cfg)
    by = {r["stage"]: r for r in rows}
    assert by["fetch"]["status"] == "skipped"
    assert by["fetch"]["detail"] == "skipped"
    assert by["transcribe"]["status"] == "skipped"
    assert by["transcribe"]["detail"] == "skipped"
    assert by["ideate"]["status"] == "ready"


def test_idea_freshness_missing_then_fresh(tmp_path):
    cfg, project = _project(tmp_path)
    narration = "the script"
    project.idea_file("01-x").write_text(_idea_md("01-x", narration, approved=True))
    m = Manifest.load(project.manifest_path)
    from shorts.ideas import sync_idea_state
    sync_idea_state(project, m)
    fr = idea_freshness(project, "01-x", m, opts_hash="deadbeef")
    assert fr == {"voice": "missing", "plan": "missing", "render": "missing"}

    project.voice_file("01-x").parent.mkdir(parents=True, exist_ok=True)
    project.voice_file("01-x").write_bytes(b"mp3")
    m.set_idea("01-x", voice={
        "path": "voice/01-x.mp3",
        "script_sha256": sha256_text(narration),
        "params_sha256": "vp",
    })
    fr = idea_freshness(project, "01-x", m, opts_hash="deadbeef")
    assert fr["voice"] == "fresh"


def test_build_snapshot_shape(tmp_path):
    cfg, project = _project(tmp_path)
    project.idea_file("01-x").write_text(_idea_md("01-x", "script one", approved=True))
    project.prompt_path.write_text(json.dumps({"prompt": "meu brief"}) + "\n")
    snap = build_snapshot(project, cfg)
    assert snap["name"] == "demo"
    assert snap["prompt"] == "meu brief"
    assert snap["job"] is None
    assert [s["stage"] for s in snap["stages"]] == [
        "fetch", "transcribe", "ideate", "voice", "plan", "render",
    ]
    assert "settings" in snap
    assert snap["settings"] == {}
    assert snap["ideas"][0]["slug"] == "01-x"
    assert snap["ideas"][0]["approved"] is True
    assert snap["ideas"][0]["narration"] == "script one"
    assert snap["ideas"][0]["description"] == "desc for 01-x"
    assert snap["ideas"][0]["tags"] == "tag one, tag two"
    assert snap["ideas"][0]["title"] == "T"
    assert snap["ideas"][0]["voice"] == "missing"
    assert snap["ideas"][0]["voice_hash"] == ""


def test_category_report_merges_folders_and_plan_beats(tmp_path):
    cfg, project = _project(tmp_path)
    (cfg.assets_dir / "neon").mkdir()
    (cfg.assets_dir / "neon" / "a.mp4").write_bytes(b"x")
    (cfg.assets_dir / "coffee").mkdir()  # folder-only, no plan beat
    project.renders_dir.mkdir(parents=True, exist_ok=True)
    project.plan_file("01-x").write_text(json.dumps({"beats": [
        {"category": "neon", "description": "neon skyline"},
        {"category": "neon", "description": "neon skyline"},  # dup collapsed
        {"category": "swamp", "description": "misty swamp"},   # no folder
        {"category": "", "description": "ignored"},
    ]}))
    rows = category_report(project, cfg)
    by = {r["category"]: r for r in rows}
    assert by["neon"] == {"category": "neon", "assets": 1, "beats": ["neon skyline"]}
    assert by["swamp"]["assets"] == 0
    assert by["coffee"]["beats"] == []
    # plan-referenced categories sort before folder-only ones
    order = [r["category"] for r in rows]
    assert order.index("neon") < order.index("coffee")
    assert order.index("swamp") < order.index("coffee")


def test_category_report_no_assets_dir(tmp_path):
    cfg, project = _project(tmp_path)
    import shutil
    shutil.rmtree(cfg.assets_dir)
    assert category_report(project, cfg) == []


def test_prompt_text_fallbacks(tmp_path):
    cfg, project = _project(tmp_path)
    from shorts.prompt import DEFAULT_IDEATE_PROMPT
    assert prompt_text(project) == DEFAULT_IDEATE_PROMPT
    project.prompt_path.write_text("{ broken")
    assert prompt_text(project) == "{ broken"


def test_list_projects(tmp_path):
    cfg, project = _project(tmp_path, "demo")
    rows = list_projects(cfg)
    assert rows[0]["name"] == "demo"
    assert "fetch" in rows[0]["stages"]


def test_publish_queue_shape(tmp_path):
    from shorts.web.state import publish_queue
    from shorts.project import Manifest, sha256_file
    cfg, project = _project(tmp_path)

    # 01-x: approved + fresh render ; 02-y: approved, not rendered
    for slug in ("01-x", "02-y"):
        project.idea_file(slug).write_text(_idea_md(slug, f"n {slug}", approved=True))
    project.render_file("01-x").parent.mkdir(parents=True, exist_ok=True)
    project.render_file("01-x").write_bytes(b"mp4")
    project.plan_file("01-x").write_text('{"beats": []}')

    m = Manifest.load(project.manifest_path)
    from shorts.web.state import _opts_hash
    m.set_idea("01-x", approved=True, script_sha256="s",
               voice={"path": "voice/01-x.mp3", "script_sha256": "s", "params_sha256": "vp"},
               plan={"path": "renders/01-x.plan.json", "script_sha256": "s",
                     "voice_params_sha256": "vp", "opts_sha256": _opts_hash(cfg)},
               render={"path": "renders/01-x.mp4",
                       "plan_sha256": sha256_file(project.plan_file("01-x"))})
    m.set_idea("02-y", approved=True, script_sha256="s")
    start = iso(
        datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
        + timedelta(days=365)
    )
    m.set_publish(start=start, interval_hours=24, weekdays=None)
    m.save(project.manifest_path)

    q = publish_queue(project, cfg)
    assert q["cadence"]["start"] == start
    by = {i["slug"]: i for i in q["items"]}
    assert by["01-x"]["render"] == "fresh"
    assert by["01-x"]["publish_at"] == start
    assert by["01-x"]["from_cadence"] is True
    assert by["01-x"]["platform"] is None
    assert by["02-y"]["render"] == "missing"
    assert by["02-y"]["publish_at"] is None


def test_publish_queue_override_and_uploaded(tmp_path):
    from shorts.web.state import publish_queue
    from shorts.project import Manifest
    cfg, project = _project(tmp_path)
    project.idea_file("01-x").write_text(_idea_md("01-x", "n", approved=True))
    m = Manifest.load(project.manifest_path)
    m.set_idea("01-x", approved=True, script_sha256="s",
               publish_at="2026-10-01T12:00:00Z",
               youtube={"video_id": "vid", "url": "https://youtu.be/vid",
                        "publish_at": "2026-10-01T12:00:00Z", "uploaded_at": "2026-09-20T00:00:00Z"})
    m.save(project.manifest_path)
    q = publish_queue(project, cfg)
    it = q["items"][0]
    assert it["publish_at_override"] == "2026-10-01T12:00:00Z"
    assert it["platform"]["video_id"] == "vid"


def test_publish_queue_tiktok_uses_planned_at_field(tmp_path):
    from shorts.web.state import publish_queue
    from shorts.project import Manifest
    cfg, project = _project(tmp_path)
    project.idea_file("01-x").write_text(_idea_md("01-x", "n", approved=True))
    m = Manifest.load(project.manifest_path)
    m.set_idea("01-x", approved=True, script_sha256="s",
               tiktok={"video_id": "vid", "publish_id": "pub",
                       "url": "https://www.tiktok.com/@x/video/vid",
                       "planned_at": "2026-10-01T12:00:00Z",
                       "uploaded_at": "2026-09-20T00:00:00Z"})
    m.save(project.manifest_path)

    q = publish_queue(project, cfg, "tiktok")
    it = q["items"][0]
    assert it["platform"]["video_id"] == "vid"
    assert it["platform"]["publish_id"] == "pub"
    assert it["platform"]["planned_at"] == "2026-10-01T12:00:00Z"
    assert it["platform"]["publish_at"] is None


def test_publish_queue_youtube_and_tiktok_are_independent(tmp_path):
    from shorts.web.state import publish_queue, _opts_hash
    from shorts.project import Manifest, sha256_file
    cfg, project = _project(tmp_path)

    for slug in ("01-x", "02-y"):
        project.idea_file(slug).write_text(_idea_md(slug, f"n {slug}", approved=True))
        project.render_file(slug).parent.mkdir(parents=True, exist_ok=True)
        project.render_file(slug).write_bytes(b"mp4")
        project.plan_file(slug).write_text('{"beats": []}')

    m = Manifest.load(project.manifest_path)
    for slug in ("01-x", "02-y"):
        m.set_idea(slug, approved=True, script_sha256="s",
                   voice={"path": f"voice/{slug}.mp3", "script_sha256": "s", "params_sha256": "vp"},
                   plan={"path": f"renders/{slug}.plan.json", "script_sha256": "s",
                         "voice_params_sha256": "vp", "opts_sha256": _opts_hash(cfg)},
                   render={"path": f"renders/{slug}.mp4",
                           "plan_sha256": sha256_file(project.plan_file(slug))})
    # 01-x already published on youtube only; 02-y already published on tiktok only.
    m.set_idea("01-x", youtube={"video_id": "vid1", "url": "https://youtu.be/vid1",
                                 "publish_at": "2026-10-01T12:00:00Z",
                                 "uploaded_at": "2026-09-20T00:00:00Z"})
    m.set_idea("02-y", tiktok={"video_id": "vid2", "publish_id": "pub2",
                                "url": "https://www.tiktok.com/@x/video/vid2",
                                "planned_at": "2026-10-02T12:00:00Z",
                                "uploaded_at": "2026-09-21T00:00:00Z"})
    m.save(project.manifest_path)

    yt_by = {i["slug"]: i for i in publish_queue(project, cfg, "youtube")["items"]}
    assert yt_by["01-x"]["platform"] is not None
    assert yt_by["02-y"]["platform"] is None  # tiktok-only publish doesn't count for youtube

    tk_by = {i["slug"]: i for i in publish_queue(project, cfg, "tiktok")["items"]}
    assert tk_by["02-y"]["platform"] is not None
    assert tk_by["01-x"]["platform"] is None  # youtube-only publish doesn't count for tiktok
