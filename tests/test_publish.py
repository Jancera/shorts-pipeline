from datetime import datetime, timedelta, timezone

import pytest

from shorts.publish import (
    cadence_from_manifest, iso, parse_iso, resolve_schedule,
)
from shorts.youtube import build_video_body

UTC = timezone.utc


def test_parse_iso_and_iso_roundtrip():
    dt = parse_iso("2026-09-10T09:00:00Z")
    assert dt == datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    assert iso(dt) == "2026-09-10T09:00:00Z"


def test_cadence_from_manifest():
    assert cadence_from_manifest({}) is None
    assert cadence_from_manifest({"interval_hours": 24}) is None
    c = cadence_from_manifest({"start": "2026-09-10T09:00:00Z"})
    assert c == {"start": datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
                 "interval_hours": 24, "weekdays": None}


def test_cadence_from_manifest_with_times():
    pub = {
        "start": "2026-09-10T09:00:00Z",
        "interval_hours": 12,
        "weekdays": [1, 2, 3],
        "times": ["18:00", "9:00", "09:00", "25:00", "bad", "12:30"],
    }
    c = cadence_from_manifest(pub)
    assert c is not None
    assert c["start"] == datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    assert c["interval_hours"] == 12
    assert c["weekdays"] == [1, 2, 3]
    assert c["times"] == ["09:00", "12:30", "18:00"]

    pub_invalid = {"start": "2026-09-10T09:00:00Z", "times": ["99:99", "invalid"]}
    c_inv = cadence_from_manifest(pub_invalid)
    assert "times" not in c_inv


def test_build_video_body_no_schedule():
    body = build_video_body(
        title="T" * 130, description="d", tags=" a, b ,, c ",
        category_id=22, publish_at=None,
    )
    assert len(body["snippet"]["title"]) == 100
    assert body["snippet"]["tags"] == ["a", "b", "c"]
    assert body["snippet"]["categoryId"] == "22"
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    assert "publishAt" not in body["status"]


def test_build_video_body_with_schedule():
    body = build_video_body(
        title="t", description="d", tags="", category_id=20,
        publish_at=datetime(2026, 9, 11, 12, 30, tzinfo=UTC),
    )
    assert body["snippet"]["tags"] == []
    assert body["status"]["publishAt"] == "2026-09-11T12:30:00Z"


def test_resolve_schedule_override_beats_cadence():
    ov = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    cad = {"start": datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
           "interval_hours": 24, "weekdays": None}
    out = resolve_schedule(["a", "b"], {"a": ov}, cad, set())
    assert out["a"] == ov
    assert out["b"] == datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def test_resolve_schedule_sequential_slots_and_taken():
    start = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    cad = {"start": start, "interval_hours": 24, "weekdays": None}
    taken = {start + timedelta(days=1)}  # slot k=1 already used
    out = resolve_schedule(["a", "b"], {}, cad, taken)
    assert out["a"] == start
    assert out["b"] == start + timedelta(days=2)  # k=1 skipped


def test_resolve_schedule_weekday_filter_skips():
    # 2026-09-12 is a Saturday (isoweekday 6); allow Mon-Fri only
    start = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    cad = {"start": start, "interval_hours": 24, "weekdays": [1, 2, 3, 4, 5]}
    out = resolve_schedule(["a"], {}, cad, set())
    assert out["a"] == datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # Monday


def test_resolve_schedule_none_without_cadence():
    assert resolve_schedule(["a"], {}, None, set()) == {"a": None}


def test_resolve_schedule_impossible_filter_hits_cap():
    cad = {"start": datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
           "interval_hours": 24, "weekdays": []}
    assert resolve_schedule(["a"], {}, cad, set()) == {"a": None}


def test_resolve_schedule_not_before_after_start_is_strictly_after_and_on_grid():
    start = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    cad = {"start": start, "interval_hours": 24, "weekdays": None}
    not_before = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)  # between k=2 and k=3
    out = resolve_schedule(["a"], {}, cad, set(), not_before=not_before)
    assert out["a"] == datetime(2026, 9, 13, 9, 0, tzinfo=UTC)  # k=3
    assert out["a"] > not_before
    assert (out["a"] - start) % timedelta(hours=24) == timedelta(0)


def test_resolve_schedule_not_before_on_a_grid_point_skips_to_next_slot():
    start = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    cad = {"start": start, "interval_hours": 24, "weekdays": None}
    not_before = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)  # exactly k=2
    out = resolve_schedule(["a"], {}, cad, set(), not_before=not_before)
    assert out["a"] == datetime(2026, 9, 13, 9, 0, tzinfo=UTC)  # strictly after


def test_resolve_schedule_not_before_before_start_behaves_like_none():
    start = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    cad = {"start": start, "interval_hours": 24, "weekdays": None}
    not_before = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    out = resolve_schedule(["a", "b"], {}, cad, set(), not_before=not_before)
    assert out["a"] == start
    assert out["b"] == start + timedelta(days=1)


def test_resolve_schedule_stale_small_interval_still_returns_future_slot():
    # cadence started years ago with a 1h interval: a naive k=0 walk would blow
    # past _K_CAP and wrongly yield None. closed-form k must land on a real slot.
    start = datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
    cad = {"start": start, "interval_hours": 1, "weekdays": None}
    not_before = datetime(2026, 9, 7, 12, 30, tzinfo=UTC)
    out = resolve_schedule(["a"], {}, cad, set(), not_before=not_before)
    assert out["a"] == datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    assert out["a"] > not_before


def test_resolve_schedule_not_before_none_is_unchanged():
    start = datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
    cad = {"start": start, "interval_hours": 24, "weekdays": None}
    assert resolve_schedule(["a"], {}, cad, set(), not_before=None) == {"a": start}


def test_resolve_schedule_with_daily_times_basic():
    # AC1: schedules 3 items across eligible slots (Day 1 10:00, Day 1 18:00, Day 2 10:00)
    # 2026-09-10 is Thursday (4), 2026-09-11 is Friday (5)
    dt = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    cad = {"start": dt, "times": ["10:00", "18:00"], "weekdays": [1, 2, 3, 4, 5]}
    out = resolve_schedule(["a", "b", "c"], {}, cad, set())
    assert out["a"] == datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    assert out["b"] == datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    assert out["c"] == datetime(2026, 9, 11, 10, 0, tzinfo=UTC)


def test_resolve_schedule_with_daily_times_not_before():
    # AC2: not_before at 12:00 on Day 1 skips 10:00 slot, assigns 18:00 as first slot
    dt = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    cad = {"start": dt, "times": ["10:00", "18:00"], "weekdays": [1, 2, 3, 4, 5]}
    not_before = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    out = resolve_schedule(["a", "b"], {}, cad, set(), not_before=not_before)
    assert out["a"] == datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    assert out["b"] == datetime(2026, 9, 11, 10, 0, tzinfo=UTC)


def test_resolve_schedule_with_daily_times_weekdays():
    # 2026-09-11 is Friday (5). 2026-09-12 (Sat) and 2026-09-13 (Sun) should be skipped.
    start = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
    cad = {"start": start, "times": ["10:00", "18:00"], "weekdays": [1, 2, 3, 4, 5]}
    out = resolve_schedule(["a", "b", "c"], {}, cad, set())
    assert out["a"] == datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    assert out["b"] == datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
    assert out["c"] == datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def test_resolve_schedule_with_daily_times_taken_and_overrides():
    start = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    cad = {"start": start, "times": ["10:00", "18:00"], "weekdays": None}
    taken_slot = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    override_slot = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)

    out = resolve_schedule(
        ["a", "b", "c"],
        {"b": override_slot},
        cad,
        taken={taken_slot},
    )
    assert out["b"] == override_slot
    # "a" skips taken (10:00) and override (18:00), taking next day 10:00
    assert out["a"] == datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    # "c" takes next day 18:00
    assert out["c"] == datetime(2026, 9, 11, 18, 0, tzinfo=UTC)


from shorts.project import Manifest, Project


def _mk_project(tmp_path, cfg):
    project = Project.create(cfg.projects_dir, "demo")
    Manifest.new("demo").save(project.manifest_path)
    return project


def _cfg(tmp_path):
    from shorts.config import (
        Config, IdeateCfg, RenderCfg, SubtitleCfg, TikTokCfg, TranscribeCfg, VoiceCfg,
        YouTubeCfg,
    )
    (tmp_path / "assets").mkdir()
    cfg = Config(
        root=tmp_path, projects_dir=tmp_path / "projects", assets_dir=tmp_path / "assets",
        aspect="9:16",
        transcribe=TranscribeCfg(model="w"), ideate=IdeateCfg(model="g", desired_video_length=90),
        voice=VoiceCfg(
            base_url="http://localhost:3900", model="m", voice="v",
            language=None, speed=1.0, instruct=None,
        ),
        render=RenderCfg(min_beat_duration=3.0, video_fit_mode="cover", subtitle=SubtitleCfg(
            enabled=True, font=None, font_size=None, primary_color=None, bold=False,
            italic=False, uppercase=False, position=None, margin_vertical=None,
            max_chars_per_line=None, max_lines=None, max_duration=None)),
        openai_api_key="sk", youtube=YouTubeCfg(
            client_secret=None, token_path=tmp_path / ".youtube_token.json",
            category_id=22),
        tiktok=TikTokCfg(
            client_key=None, client_secret=None,
            token_path=tmp_path / ".tiktok_token.json", privacy_level="SELF_ONLY",
            disable_duet=False, disable_stitch=False, disable_comment=False,
            is_aigc=False,
        ),
    )
    return cfg


def _fake_youtube_target(*, upload=None, get_credentials=None, category_id=22):
    from functools import partial

    from shorts.publish_target import PublishTarget

    def default_upload(client, *, mp4_path, body):
        return {"video_id": "vid123", "url": "https://youtu.be/vid123"}

    return PublishTarget(
        key="youtube", label="YouTube", supports_scheduling=True,
        is_configured=lambda c: True,
        get_credentials=get_credentials or (lambda c: object()),
        authorize=lambda c: object(),
        account_label=lambda creds: "chan",
        build_client=lambda creds: creds,
        build_body=partial(build_video_body, category_id=category_id),
        upload=upload or default_upload,
        parse_upload_error=lambda exc: {"message": str(exc), "abort_batch": False},
    )


IDEA_MD = (
    "---\nslug: {s}\ntitle: {t}\n---\n\n- [x] Approved\n\n"
    "## Description\n\ndesc {s}\n\n## Tags\n\ntag one, tag two\n\n"
    "## Hook\n\nh\n\n## Narration\n\nn {s}\n\n## Notes\n\nx\n"
)


def _fresh_rendered_idea(project, slug, title="T"):
    project.idea_file(slug).write_text(IDEA_MD.format(s=slug, t=title))
    project.render_file(slug).parent.mkdir(parents=True, exist_ok=True)
    project.render_file(slug).write_bytes(b"mp4")
    project.plan_file(slug).write_text('{"beats": []}')
    from shorts.project import sha256_file
    m = Manifest.load(project.manifest_path)
    m.set_idea(slug, approved=True, script_sha256="s",
               voice={"path": f"voice/{slug}.mp3", "script_sha256": "s", "params_sha256": "vp"},
               plan={"path": f"renders/{slug}.plan.json", "script_sha256": "s",
                     "voice_params_sha256": "vp", "opts_sha256": _opts(m, project)},
               render={"path": f"renders/{slug}.mp4",
                       "plan_sha256": sha256_file(project.plan_file(slug))})
    m.save(project.manifest_path)


def _opts(_m, _p):
    # opts_sha256 doesn't affect render freshness; any stable value works here
    return "x"


def test_run_uploads_eligible_and_records(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x", "First")
    _fresh_rendered_idea(project, "02-y", "Second")

    import shorts.publish as pub
    seen = []
    def fake_upload(client, *, mp4_path, body):
        seen.append((mp4_path.name, body["snippet"]["title"], body["status"].get("publishAt")))
        return {"video_id": f"v{len(seen)}", "url": f"https://youtu.be/v{len(seen)}",
                "privacy": "private"}
    target = _fake_youtube_target(upload=fake_upload)

    start_dt = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(
        days=365
    )
    start = iso(start_dt)
    m = Manifest.load(project.manifest_path)
    m.set_publish(start=start, interval_hours=24, weekdays=None)
    m.save(project.manifest_path)

    pub.run(project, cfg, target)

    assert [s[0] for s in seen] == ["01-x.mp4", "02-y.mp4"]
    assert seen[0][2] == start
    assert seen[1][2] == iso(start_dt + timedelta(days=1))
    back = Manifest.load(project.manifest_path)
    assert back.get_idea("01-x")["youtube"]["video_id"] == "v1"
    assert back.get_idea("01-x")["youtube"]["privacy"] == "private"


def test_run_skips_already_uploaded_unless_force(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")
    m = Manifest.load(project.manifest_path)
    m.set_idea("01-x", youtube={"video_id": "old", "url": "u", "publish_at": None})
    m.save(project.manifest_path)

    import shorts.publish as pub
    calls = []
    def fake_upload(client, *, mp4_path, body):
        calls.append(1)
        return {"video_id": "new", "url": "u2"}
    target = _fake_youtube_target(upload=fake_upload)

    pub.run(project, cfg, target)                 # skipped
    assert calls == []
    pub.run(project, cfg, target, force=True)     # re-uploaded
    assert calls == [1]


def test_run_propagates_auth_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")
    import shorts.publish as pub
    from shorts.youtube import YouTubeAuthError
    def boom(_c): raise YouTubeAuthError("expired", reason="expired")
    target = _fake_youtube_target(get_credentials=boom)
    with pytest.raises(YouTubeAuthError):
        pub.run(project, cfg, target)


def test_run_isolates_per_idea_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")
    _fresh_rendered_idea(project, "02-y")
    import shorts.publish as pub

    class Err(Exception):
        def __init__(self): self.resp = type("R", (), {"status": 400})(); self.content = b'{"error":{"message":"bad"}}'

    def flaky(client, *, mp4_path, body):
        if mp4_path.name == "01-x.mp4":
            raise Err()
        return {"video_id": "v2", "url": "https://youtu.be/v2"}
    target = _fake_youtube_target(upload=flaky)

    with pytest.raises(SystemExit):
        pub.run(project, cfg, target)
    back = Manifest.load(project.manifest_path)
    assert "youtube" not in back.get_idea("01-x")
    assert back.get_idea("02-y")["youtube"]["video_id"] == "v2"


def test_run_isolates_per_idea_failure_aborts_batch_on_quota(tmp_path, monkeypatch):
    # equivalent to the old HttpError-403-quota abort-batch branch, now driven
    # by target.parse_upload_error()'s abort_batch flag instead of an
    # isinstance(exc, HttpError) check baked into publish.run().
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")
    _fresh_rendered_idea(project, "02-y")
    import shorts.publish as pub
    from functools import partial
    from shorts.publish_target import PublishTarget

    calls = []

    def flaky(client, *, mp4_path, body):
        calls.append(mp4_path.name)
        if mp4_path.name == "01-x.mp4":
            raise RuntimeError("quota exceeded")
        # 02-y would succeed if ever attempted -- proving a wrongly-continuing
        # implementation would leave a "youtube" key that our assertions below
        # would then catch.
        return {"video_id": "v2", "url": "https://youtu.be/v2"}

    target = PublishTarget(
        key="youtube", label="YouTube", supports_scheduling=True,
        is_configured=lambda c: True,
        get_credentials=lambda c: object(), authorize=lambda c: object(),
        account_label=lambda creds: "chan", build_client=lambda creds: creds,
        build_body=partial(build_video_body, category_id=22),
        upload=flaky,
        parse_upload_error=lambda exc: {"message": str(exc), "abort_batch": True},
    )

    with pytest.raises(SystemExit):
        pub.run(project, cfg, target)
    # The second idea's upload must never even be attempted: this is what
    # distinguishes "aborted after the quota error" from "kept going
    # regardless of abort_batch" (which would call flaky twice).
    assert calls == ["01-x.mp4"]
    back = Manifest.load(project.manifest_path)
    assert "youtube" not in back.get_idea("01-x")
    assert "youtube" not in back.get_idea("02-y")  # aborted before 02-y ran


def test_run_slug_upload_matches_web_queue_preview(tmp_path, monkeypatch):
    # T19: `publish <n> --slug 02-y` must land on the slot the web queue previewed
    # for 02-y (k=1), not `start` (k=0).
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x", "First")
    _fresh_rendered_idea(project, "02-y", "Second")

    start_dt = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(
        days=365
    )
    start = iso(start_dt)
    m = Manifest.load(project.manifest_path)
    m.set_publish(start=start, interval_hours=24, weekdays=None)
    m.save(project.manifest_path)

    from shorts.web.state import publish_queue
    preview = {it["slug"]: it["publish_at"] for it in publish_queue(project, cfg)["items"]}
    expected_slot = iso(start_dt + timedelta(days=1))
    assert preview["02-y"] == expected_slot

    import shorts.publish as pub
    seen = []
    def fake_upload(client, *, mp4_path, body):
        seen.append((mp4_path.name, body["status"].get("publishAt")))
        return {"video_id": "v", "url": "https://youtu.be/v"}
    target = _fake_youtube_target(upload=fake_upload)

    pub.run(project, cfg, target, slugs=["02-y"])

    assert [s[0] for s in seen] == ["02-y.mp4"]
    assert seen[0][1] == expected_slot
    back = Manifest.load(project.manifest_path)
    assert back.get_idea("02-y")["youtube"]["publish_at"] == preview["02-y"]


def test_run_missing_idea_file_fails_that_slug_only(tmp_path, monkeypatch):
    # I3: a deleted idea .md must not abort the batch with a traceback.
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")
    _fresh_rendered_idea(project, "02-y")
    project.idea_file("01-x").unlink()

    import shorts.publish as pub
    seen = []
    def fake_upload(client, *, mp4_path, body):
        seen.append(body)
        return {"video_id": "v2", "url": "https://youtu.be/v2"}
    target = _fake_youtube_target(upload=fake_upload)

    with pytest.raises(SystemExit):
        pub.run(project, cfg, target)
    back = Manifest.load(project.manifest_path)
    assert "youtube" not in back.get_idea("01-x")
    assert back.get_idea("02-y")["youtube"]["video_id"] == "v2"


def test_run_blank_title_falls_back_to_slug(tmp_path, monkeypatch):
    # I4: a present-but-blank frontmatter title must not reach YouTube as "".
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x", title="")

    import shorts.publish as pub
    seen = []
    def fake_upload(client, *, mp4_path, body):
        seen.append(body["snippet"]["title"])
        return {"video_id": "v", "url": "https://youtu.be/v"}
    target = _fake_youtube_target(upload=fake_upload)

    pub.run(project, cfg, target)
    assert seen == ["01-x"]


def _fake_tiktok_target(*, upload=None):
    from functools import partial

    from shorts.publish_target import PublishTarget
    from shorts.tiktok import build_post_info

    def default_upload(client, *, mp4_path, body):
        return {"publish_id": "pub123", "status": "PROCESSING_UPLOAD"}

    return PublishTarget(
        key="tiktok", label="TikTok", supports_scheduling=False,
        is_configured=lambda c: True,
        get_credentials=lambda c: object(),
        authorize=lambda c: object(),
        account_label=lambda creds: "tiktok_user",
        build_client=lambda creds: creds,
        build_body=partial(
            build_post_info,
            privacy_level="SELF_ONLY",
            disable_duet=False,
            disable_stitch=False,
            disable_comment=False,
            is_aigc=False,
        ),
        upload=upload or default_upload,
        parse_upload_error=lambda exc: {"message": str(exc), "abort_batch": False},
    )


def test_run_tiktok_records_under_tiktok_key_with_planned_at(tmp_path):
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")

    import shorts.publish as pub
    target = _fake_tiktok_target()

    pub.run(project, cfg, target, slugs=None, force=False)

    manifest = Manifest.load(project.manifest_path)
    tiktok_record = manifest.get_idea("01-x").get("tiktok")
    assert tiktok_record is not None
    assert "publish_id" in tiktok_record
    assert "status" in tiktok_record
    assert "uploaded_at" in tiktok_record
    assert "planned_at" in tiktok_record
    assert "publish_at" not in tiktok_record


def test_run_youtube_and_tiktok_do_not_collide_for_same_idea(tmp_path):
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")

    import shorts.publish as pub

    # First, publish to YouTube
    youtube_target = _fake_youtube_target()
    pub.run(project, cfg, youtube_target, slugs=None, force=False)

    # Reload manifest to see YouTube publication
    manifest = Manifest.load(project.manifest_path)
    assert "youtube" in manifest.get_idea("01-x")

    # Then, publish the same idea to TikTok
    tiktok_target = _fake_tiktok_target()
    pub.run(project, cfg, tiktok_target, slugs=None, force=False)

    # Both should be present in the final manifest
    manifest = Manifest.load(project.manifest_path)
    idea = manifest.get_idea("01-x")
    assert "youtube" in idea
    assert idea["youtube"]["video_id"] == "vid123"
    assert "tiktok" in idea
    assert idea["tiktok"]["publish_id"] == "pub123"


def test_run_tiktok_skips_already_published_unless_force(tmp_path):
    cfg = _cfg(tmp_path)
    project = _mk_project(tmp_path, cfg)
    _fresh_rendered_idea(project, "01-x")
    m = Manifest.load(project.manifest_path)
    m.set_idea("01-x", tiktok={"publish_id": "old", "status": "PUBLISHED", "planned_at": None})
    m.save(project.manifest_path)

    import shorts.publish as pub
    calls = []
    def fake_upload(client, *, mp4_path, body):
        calls.append(1)
        return {"publish_id": "new", "status": "PROCESSING_UPLOAD"}
    target = _fake_tiktok_target(upload=fake_upload)

    pub.run(project, cfg, target)                 # skipped
    assert calls == []
    pub.run(project, cfg, target, force=True)     # re-uploaded
    assert calls == [1]
