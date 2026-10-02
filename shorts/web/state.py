from __future__ import annotations

import json
from pathlib import Path

from shorts.config import Config
from shorts.ideas import sync_idea_state
from shorts.markdown import parse_idea_file
from shorts.project import Manifest, Project, sha256_file
from shorts.prompt import DEFAULT_IDEATE_PROMPT
from shorts.stages.plan import plan_opts_hash, subtitle_plan_args


def _opts_hash(config: Config) -> str:
    return plan_opts_hash(
        min_beat_duration=config.render.min_beat_duration,
        subtitle_args=subtitle_plan_args(config.render.subtitle),
    )


def prompt_text(project: Project) -> str:
    path = project.prompt_path
    if not path.exists():
        return DEFAULT_IDEATE_PROMPT
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        value = str(data["prompt"]) if isinstance(data, dict) else ""
        return value or raw
    except (json.JSONDecodeError, KeyError):
        return raw


def idea_freshness(
    project: Project, slug: str, manifest: Manifest, opts_hash: str
) -> dict:
    idea = manifest.get_idea(slug)
    script = idea.get("script_sha256")

    voice = idea.get("voice") or {}
    if not voice or not project.voice_file(slug).exists():
        voice_state = "missing"
    elif voice.get("script_sha256") == script:
        voice_state = "fresh"
    else:
        voice_state = "stale"

    plan = idea.get("plan") or {}
    plan_file = project.plan_file(slug)
    if not plan or not plan_file.exists():
        plan_state = "missing"
    elif (
        plan.get("script_sha256") == voice.get("script_sha256")
        and plan.get("voice_params_sha256") == voice.get("params_sha256")
        and plan.get("opts_sha256") == opts_hash
    ):
        plan_state = "fresh"
    else:
        plan_state = "stale"

    render = idea.get("render") or {}
    if not render or not project.render_file(slug).exists():
        render_state = "missing"
    elif plan_file.exists() and render.get("plan_sha256") == sha256_file(plan_file):
        render_state = "fresh"
    else:
        render_state = "stale"

    return {"voice": voice_state, "plan": plan_state, "render": render_state}


def _agg(states: list[str]) -> str:
    """done if all fresh, else stale.

    The empty-list branch is a defensive fallback: every call site guards on a
    non-empty list before calling this, so ``"ready"`` is not returned in normal
    flow.
    """
    if not states:
        return "ready"
    return "done" if all(s == "fresh" for s in states) else "stale"


def stage_rows(
    project: Project, manifest: Manifest, config: Config
) -> list[dict]:
    opts_hash = _opts_hash(config)
    slugs = sorted(manifest.ideas)
    approved = [s for s in slugs if manifest.get_idea(s).get("approved")]
    fr = {s: idea_freshness(project, s, manifest, opts_hash) for s in slugs}

    def at(stage: str):
        return (manifest.get_stage(stage) or {}).get("at")

    rows: list[dict] = []

    # fetch
    fetch_stage = manifest.get_stage("fetch")
    if fetch_stage and fetch_stage.get("status") == "skipped":
        rows.append({"stage": "fetch", "status": "skipped", "at": at("fetch"), "detail": "skipped"})
        fetch_done = True
    elif manifest.is_stage_done("fetch") and project.video_path.exists():
        rows.append({"stage": "fetch", "status": "done", "at": at("fetch"), "detail": ""})
        fetch_done = True
    else:
        rows.append({"stage": "fetch", "status": "ready", "at": at("fetch"),
                     "detail": "no source video"})
        fetch_done = False

    # transcribe
    transcribe_stage = manifest.get_stage("transcribe")
    if transcribe_stage and transcribe_stage.get("status") == "skipped":
        rows.append({"stage": "transcribe", "status": "skipped", "at": at("transcribe"), "detail": "skipped"})
        transcribe_done = True
    elif not fetch_done:
        rows.append({"stage": "transcribe", "status": "blocked", "at": at("transcribe"),
                     "detail": "needs fetch"})
        transcribe_done = False
    elif not (manifest.is_stage_done("transcribe") and project.transcript_txt_path.exists()):
        rows.append({"stage": "transcribe", "status": "ready", "at": at("transcribe"),
                     "detail": ""})
        transcribe_done = False
    else:
        recorded = (manifest.get_stage("transcribe") or {}).get("audio_sha256")
        current = sha256_file(project.audio_path) if project.audio_path.exists() else recorded
        status = "done" if recorded == current else "stale"
        rows.append({"stage": "transcribe", "status": status, "at": at("transcribe"),
                     "detail": "" if status == "done" else "audio changed"})
        transcribe_done = rows[-1]["status"] == "done"

    # ideate
    if not transcribe_done:
        rows.append({"stage": "ideate", "status": "blocked", "at": at("ideate"),
                     "detail": "needs transcribe"})
    elif not manifest.is_stage_done("ideate"):
        rows.append({"stage": "ideate", "status": "ready", "at": at("ideate"), "detail": ""})
    else:
        ideate_stage = manifest.get_stage("ideate") or {}
        recorded = ideate_stage.get("prompt_sha256")
        recorded_length = ideate_stage.get("desired_length")
        from shorts.project import sha256_text
        current = sha256_text(prompt_text(project))
        
        target_length = manifest.get_setting("desired_length")
        if not isinstance(target_length, int) or isinstance(target_length, bool) or target_length < 10:
            target_length = config.ideate.desired_video_length
            
        recorded_count = ideate_stage.get("count")
        target_count = manifest.get_setting("count")
        if not isinstance(target_count, int) or isinstance(target_count, bool) or target_count < 1:
            target_count = 5
        
        if recorded not in (None, current):
            status = "stale"
            detail = "prompt.json changed"
        elif recorded_length not in (None, target_length):
            status = "stale"
            detail = "desired length changed"
        elif recorded_count not in (None, target_count):
            status = "stale"
            detail = "target count changed"
        else:
            status = "done"
            detail = f"{len(slugs)} ideas"
            
        rows.append({"stage": "ideate", "status": status, "at": at("ideate"),
                     "detail": detail})
    ideate_done = rows[-1]["status"] == "done"

    # voice
    if not ideate_done:
        rows.append({"stage": "voice", "status": "blocked", "at": at("voice"),
                     "detail": "needs ideate"})
    elif not approved:
        rows.append({"stage": "voice", "status": "ready", "at": at("voice"),
                     "detail": "no approved ideas"})
    else:
        states = [fr[s]["voice"] for s in approved]
        status = _agg(states)
        need = sum(1 for x in states if x != "fresh")
        rows.append({"stage": "voice", "status": status, "at": at("voice"),
                     "detail": "" if status == "done"
                     else f"{need} of {len(approved)} approved need audio"})

    # plan
    have_voice = [s for s in approved if fr[s]["voice"] == "fresh"]
    if not ideate_done:
        rows.append({"stage": "plan", "status": "blocked", "at": at("plan"),
                     "detail": "needs ideate"})
    elif not have_voice:
        rows.append({"stage": "plan", "status": "blocked", "at": at("plan"),
                     "detail": "needs voice"})
    else:
        states = [fr[s]["plan"] for s in have_voice]
        status = _agg(states)
        need = sum(1 for x in states if x != "fresh")
        rows.append({"stage": "plan", "status": status, "at": at("plan"),
                     "detail": "" if status == "done"
                     else f"{need} of {len(have_voice)} need a plan"})

    # render
    have_plan = [s for s in have_voice if fr[s]["plan"] in ("fresh", "stale")]
    if not have_plan:
        rows.append({"stage": "render", "status": "blocked", "at": at("render"),
                     "detail": "needs plan"})
    else:
        states = [fr[s]["render"] for s in have_plan]
        status = _agg(states)
        need = sum(1 for x in states if x != "fresh")
        rows.append({"stage": "render", "status": status, "at": at("render"),
                     "detail": "" if status == "done"
                     else f"{need} of {len(have_plan)} need a render"})

    return rows


def build_snapshot(project: Project, config: Config) -> dict:
    manifest = Manifest.load(project.manifest_path)
    parsed = dict(sync_idea_state(project, manifest))
    manifest.save(project.manifest_path)
    opts_hash = _opts_hash(config)

    ideas = []
    for slug in sorted(manifest.ideas):
        idea = manifest.get_idea(slug)
        p = parsed.get(slug)
        plan_file = project.plan_file(slug)
        plan_json = None
        if plan_file.exists():
            try:
                plan_json = json.loads(plan_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                plan_json = None
        fr = idea_freshness(project, slug, manifest, opts_hash)
        ideas.append({
            "slug": slug,
            "title": (p.frontmatter.get("title") if p else "") or slug,
            "description": p.description if p else "",
            "tags": p.tags if p else "",
            "approved": bool(idea.get("approved")),
            "narration": p.narration if p else "",
            "voice": fr["voice"],
            "voice_hash": str(project.voice_file(slug).stat().st_mtime) if project.voice_file(slug).exists() else "",
            "plan": fr["plan"],
            "render": fr["render"],
            "plan_json": plan_json,
        })

    return {
        "name": project.name,
        "source": {
            "url": manifest.source.get("url", ""),
            "title": manifest.source.get("title", ""),
            "video_id": manifest.source.get("video_id", ""),
        },
        "settings": manifest.settings,
        "stages": stage_rows(project, manifest, config),
        "prompt": prompt_text(project),
        "ideas": ideas,
        "job": None,
        "default_desired_length": (
            manifest.get_setting("desired_length")
            if isinstance(manifest.get_setting("desired_length"), int) and not isinstance(manifest.get_setting("desired_length"), bool) and manifest.get_setting("desired_length") >= 10
            else config.ideate.desired_video_length
        ),
    }


def list_projects(config: Config) -> list[dict]:
    out = []
    for project in Project.list_all(config.projects_dir):
        try:
            manifest = Manifest.load(project.manifest_path)
            rows = stage_rows(project, manifest, config)
        except (json.JSONDecodeError, KeyError, FileNotFoundError):
            # a single corrupt/missing manifest.json must not 500 the whole list
            continue
        out.append({
            "name": project.name,
            "title": manifest.source.get("title", ""),
            "updated_at": _mtime_iso(project.manifest_path),
            "stages": {r["stage"]: r["status"] for r in rows},
        })
    return out


def publish_queue(project: Project, config: Config, platform: str = "youtube") -> dict:
    from datetime import datetime, timezone

    # function-local: keeps import cost out of routes that don't touch publish/scheduling
    from shorts.publish import cadence_from_manifest, iso, parse_iso, resolve_schedule

    key = platform
    at_field = "publish_at" if platform == "youtube" else "planned_at"

    manifest = Manifest.load(project.manifest_path)
    sync_idea_state(project, manifest)
    manifest.save(project.manifest_path)
    opts_hash = _opts_hash(config)

    slugs = sorted(manifest.ideas)
    approved = [s for s in slugs if manifest.get_idea(s).get("approved")]
    fr = {s: idea_freshness(project, s, manifest, opts_hash) for s in approved}

    overrides: dict = {}
    taken: set = set()
    for s in slugs:
        e = manifest.get_idea(s)
        if e.get("publish_at"):
            overrides[s] = parse_iso(e["publish_at"])
            taken.add(overrides[s])
        plat = e.get(key) or {}
        if plat.get(at_field):
            taken.add(parse_iso(plat[at_field]))

    slot_slugs = [
        s for s in approved
        if fr[s]["render"] == "fresh" and not manifest.get_idea(s).get(key)
    ]
    sched = resolve_schedule(
        slot_slugs,
        {s: overrides[s] for s in slot_slugs if s in overrides},
        cadence_from_manifest(manifest.get_publish()),
        taken,
        not_before=datetime.now(timezone.utc),
    )

    items = []
    for s in approved:
        e = manifest.get_idea(s)
        plat = e.get(key)
        override_iso = e.get("publish_at") or None
        resolved = None
        from_cadence = False
        if s in sched and sched[s] is not None:
            resolved = iso(sched[s])
            from_cadence = override_iso is None
        elif override_iso:
            resolved = override_iso
        parsed = parse_idea_file(project.idea_file(s).read_text(encoding="utf-8")) \
            if project.idea_file(s).exists() else None
        items.append({
            "slug": s,
            "title": (parsed.frontmatter.get("title") if parsed else "") or s,
            "render": fr[s]["render"],
            "publish_at_override": override_iso,
            "publish_at": resolved,
            "from_cadence": from_cadence,
            "platform": {k2: plat.get(k2) for k2 in (
                "video_id", "publish_id", "url", "publish_at", "planned_at", "uploaded_at",
            )} if plat else None,
        })

    return {"cadence": manifest.get_publish() or None, "items": items}


def _mtime_iso(path: Path) -> str:
    from datetime import datetime, timezone
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _asset_counts(assets_dir: Path) -> dict[str, int]:
    """Immediate subfolders of the asset library -> count of files directly inside."""
    out: dict[str, int] = {}
    if not assets_dir.is_dir():
        return out
    for child in sorted(assets_dir.iterdir()):
        if child.is_dir():
            out[child.name] = sum(1 for f in child.iterdir() if f.is_file())
    return out


def category_report(project: Project, config: Config) -> list[dict]:
    """Per asset category: how many files are on hand, and which plan beats need it.

    Merges the asset library's folders with every ``category``/``description``
    pair found in this project's ``renders/*.plan.json`` beats. A category a
    plan references but the library has no folder for shows ``assets: 0``.
    Categories the plans actually use are listed first.
    """
    counts = _asset_counts(config.assets_dir)
    beats: dict[str, list[str]] = {}
    if project.renders_dir.is_dir():
        for plan_path in sorted(project.renders_dir.glob("*.plan.json")):
            try:
                data = json.loads(plan_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for beat in data.get("beats", []) if isinstance(data, dict) else []:
                cat = beat.get("category")
                if not cat:
                    continue
                desc = (beat.get("description") or "").strip()
                if desc and desc not in beats.setdefault(cat, []):
                    beats[cat].append(desc)
                beats.setdefault(cat, [])

    rows = [
        {"category": cat, "assets": counts.get(cat, 0), "beats": beats.get(cat, [])}
        for cat in set(counts) | set(beats)
    ]
    rows.sort(key=lambda r: (not r["beats"], r["category"]))
    return rows
