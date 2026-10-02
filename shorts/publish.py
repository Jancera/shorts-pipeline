from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from shorts.ideas import sync_idea_state
from shorts.markdown import parse_idea_file
from shorts.project import Manifest, utcnow_iso
from shorts.publish_target import PublishTarget
from shorts.stages.plan import plan_opts_hash, subtitle_plan_args

_K_CAP = 3650


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def cadence_from_manifest(pub: dict) -> dict | None:
    if not pub or not pub.get("start"):
        return None
    res = {
        "start": parse_iso(pub["start"]),
        "interval_hours": int(pub.get("interval_hours", 24)),
        "weekdays": pub.get("weekdays"),
    }
    raw_times = pub.get("times")
    if isinstance(raw_times, list):
        valid_times = set()
        for t in raw_times:
            if isinstance(t, str):
                m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", t.strip())
                if m:
                    valid_times.add(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
        if valid_times:
            res["times"] = sorted(valid_times)
    return res


def resolve_schedule(
    slugs: list[str],
    overrides: dict[str, datetime],
    cadence: dict | None,
    taken: set[datetime],
    not_before: datetime | None = None,
) -> dict[str, datetime | None]:
    used = set(taken)
    out: dict[str, datetime | None] = {}
    start = cadence["start"] if cadence else None
    weekdays = cadence["weekdays"] if cadence else None
    times = cadence.get("times") if cadence else None

    if times:
        used.update(overrides.values())
        for slug in slugs:
            if slug in overrides:
                out[slug] = overrides[slug]
            else:
                out[slug] = None

        if start is None:
            return out

        pending = [s for s in slugs if s not in overrides]
        if not pending:
            return out

        tz = start.tzinfo
        nb = not_before
        if nb is not None:
            if tz is not None and nb.tzinfo is None:
                nb = nb.replace(tzinfo=tz)
            elif tz is None and nb.tzinfo is not None:
                tz = nb.tzinfo
            nb_in_tz = nb.astimezone(tz) if tz else nb
            start_date = nb_in_tz.date() if nb_in_tz.date() > start.date() else start.date()
        else:
            start_date = start.date()

        parsed_times = []
        for t_str in set(times):
            h, m = map(int, t_str.split(":"))
            parsed_times.append((h, m))
        parsed_times.sort()

        pending_idx = 0
        num_pending = len(pending)

        for day_idx in range(_K_CAP):
            if pending_idx >= num_pending:
                break
            day = start_date + timedelta(days=day_idx)
            if weekdays is not None and day.isoweekday() not in weekdays:
                continue
            for hour, minute in parsed_times:
                slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                if slot < start:
                    continue
                if nb is not None and slot <= nb:
                    continue
                if slot in used:
                    continue
                slug = pending[pending_idx]
                out[slug] = slot
                used.add(slot)
                pending_idx += 1
                if pending_idx >= num_pending:
                    break

        return out

    k = 0
    interval = timedelta(hours=cadence["interval_hours"]) if cadence else None
    if start is not None and not_before is not None:
        delta = (not_before - start) / interval  # timedelta / timedelta -> float
        if delta >= 0:
            k = int(delta) + 1  # first grid slot strictly after not_before
    stop = k + _K_CAP
    for slug in slugs:
        if slug in overrides:
            out[slug] = overrides[slug]
            used.add(overrides[slug])
            continue
        if start is None:
            out[slug] = None
            continue
        chosen = None
        while k < stop:
            slot = start + interval * k
            k += 1
            if slot in used:
                continue
            if weekdays is not None and slot.isoweekday() not in weekdays:
                continue
            chosen = slot
            break
        out[slug] = chosen
        if chosen is not None:
            used.add(chosen)
    return out


def run(
    project, config, target: "PublishTarget", *, slugs: list[str] | None = None,
    force: bool = False,
) -> None:
    from shorts.web.state import idea_freshness

    manifest = Manifest.load(project.manifest_path)
    sync_idea_state(project, manifest)
    opts_hash = plan_opts_hash(
        min_beat_duration=config.render.min_beat_duration,
        subtitle_args=subtitle_plan_args(config.render.subtitle),
    )
    all_slugs = sorted(manifest.ideas)

    def eligible(s: str) -> bool:
        e = manifest.get_idea(s)
        if not e.get("approved"):
            return False
        return idea_freshness(project, s, manifest, opts_hash)["render"] == "fresh"

    at_field = "publish_at" if target.supports_scheduling else "planned_at"

    # Full pending set, computed the same way as web.state.publish_queue's
    # slot_slugs: approved + fresh render + not yet uploaded. Independent of the
    # --slug filter and of force, so a per-row upload lands on the same slot the
    # web queue previewed for that slug.
    pending = [
        s for s in all_slugs
        if eligible(s) and not manifest.get_idea(s).get(target.key)
    ]

    candidates = [s for s in all_slugs if eligible(s)]
    if slugs:
        want = set(slugs)
        candidates = [s for s in candidates if s in want]
    if not force:
        candidates = [s for s in candidates if not manifest.get_idea(s).get(target.key)]

    if not candidates:
        print("publish: nothing to upload")
        return

    creds = target.get_credentials(config)
    client = target.build_client(creds)

    overrides: dict[str, datetime] = {}
    taken: set[datetime] = set()
    for s in all_slugs:
        e = manifest.get_idea(s)
        if e.get("publish_at"):
            overrides[s] = parse_iso(e["publish_at"])
            taken.add(overrides[s])
        pl = e.get(target.key) or {}
        if pl.get(at_field):
            taken.add(parse_iso(pl[at_field]))

    schedule = resolve_schedule(
        pending,
        {s: overrides[s] for s in pending if s in overrides},
        cadence_from_manifest(manifest.get_publish()),
        taken,
        not_before=datetime.now(timezone.utc),
    )

    uploaded = failed = 0
    for s in candidates:
        if not project.idea_file(s).exists():
            print(f"publish: {s} FAILED - no idea file")
            failed += 1
            continue
        parsed = parse_idea_file(project.idea_file(s).read_text(encoding="utf-8"))
        # explicit per-idea publish_at always wins, even for forced re-uploads
        # (which are absent from `pending`, hence from `schedule`)
        at = overrides.get(s) or schedule.get(s)
        body = target.build_body(
            title=parsed.frontmatter.get("title") or s,
            description=parsed.description,
            tags=parsed.tags,
            publish_at=at,
        )
        try:
            res = target.upload(client, mp4_path=project.render_file(s), body=body)
        except Exception as exc:  # a batch must not die on one un-typed upload error
            info = target.parse_upload_error(exc)
            print(f"publish: {s} FAILED {info['message']}")
            failed += 1
            if info.get("abort_batch"):
                print(f"publish: aborting - {uploaded} uploaded, rest deferred")
                break
            continue
        record = dict(res)
        record["uploaded_at"] = utcnow_iso()
        record[at_field] = iso(at) if at else None
        manifest.set_idea(s, **{target.key: record})
        manifest.save(project.manifest_path)
        uploaded += 1
        print(f"publish: {s} -> {res.get('url') or res.get('publish_id')}")

    print(f"publish: uploaded {uploaded} video(s)")
    if failed:
        raise SystemExit(1)
