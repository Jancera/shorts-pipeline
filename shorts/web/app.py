from __future__ import annotations

from dataclasses import asdict
import json
import os
import queue
import re
import sys
import tempfile
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from shorts.config import Config, ConfigError, load_config, update_subtitle_config
from shorts.web.edits import (
    EditError, apply_idea_edit, build_prompt_json, validate_plan_text,
)
from shorts.markdown import _section, parse_idea_file, set_approved
from shorts.openai_helpers import get_client, refine_idea_text
from shorts.project import Manifest, Project, slugify
from shorts.prompt import ensure_prompt_file
from shorts.web.jobs import (
    ALLOWED_STAGES, JobBusy, JobRunner, _HEARTBEAT_SECONDS, sse_format, stage_argv,
)
from shorts.web.state import build_snapshot, category_report, list_projects, publish_queue

_STATIC = Path(__file__).parent / "static"
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _target_for(platform: str, config):
    if platform == "youtube":
        from shorts.youtube import target
    elif platform == "tiktok":
        from shorts.tiktok import target
    else:
        raise ValueError(platform)
    return target(config)


def _atomic_write(path: Path, text: str) -> None:
    tmp_path: Path | None = None
    try:
        stat = os.stat(path) if path.exists() else None
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        if stat is not None:
            os.chmod(tmp_path, stat.st_mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _json_error(status: int, message: str):
    return jsonify({"error": message}), status


def _validate_count(count_raw: object) -> int:
    if isinstance(count_raw, bool) or not isinstance(count_raw, (int, str)):
        raise ValueError("count must be an integer >= 1")
    try:
        count_val = int(count_raw)
    except ValueError:
        raise ValueError("count must be an integer >= 1")
    if count_val < 1:
        raise ValueError("count must be an integer >= 1")
    return count_val


def _load_project(config: Config, name: str) -> Project:
    return Project.load(config.projects_dir, name)


def create_app(config: Config) -> Flask:
    app = Flask(__name__, static_folder=None)
    runner = JobRunner(config.root)
    app.config["SHORTS_CONFIG"] = config
    app.config["ROOT_DIR"] = config.root
    app.config["JOB_RUNNER"] = runner

    def _runner():
        return app.config["JOB_RUNNER"]

    @app.get("/api/config/subtitles")
    def api_get_subtitles_config():
        cfg = app.config.get("SHORTS_CONFIG", config)
        return jsonify(asdict(cfg.render.subtitle))

    @app.put("/api/config/subtitles")
    def api_put_subtitles_config():
        payload = request.get_json(silent=True)
        if payload is None:
            return _json_error(400, "request body must be a JSON object")
        root_dir = app.config.get("ROOT_DIR") or getattr(app.config.get("SHORTS_CONFIG"), "root", None)
        try:
            update_subtitle_config(payload, root=root_dir)
            new_config = load_config(root_dir)
            app.config["SHORTS_CONFIG"] = new_config
            return jsonify(asdict(new_config.render.subtitle))
        except ConfigError as exc:
            return _json_error(422, str(exc))

    @app.get("/")
    def index():
        return send_from_directory(_STATIC, "index.html")

    @app.get("/api/projects")
    def api_projects():
        return jsonify(list_projects(config))

    @app.get("/api/projects/<name>")
    def api_project(name: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        return _snapshot(project)

    @app.get("/api/jobs/current")
    def api_job_current():
        return jsonify(_runner().state())

    def _snapshot(project):
        try:
            snap = build_snapshot(project, config)
        except FileNotFoundError:
            # manifest.json not written yet (e.g. fetch subprocess still running)
            return _json_error(404, f"no such project: {project.name}")
        except (json.JSONDecodeError, KeyError):
            return _json_error(422, f"{project.name}/manifest.json is not valid JSON")
        snap["job"] = _runner().state() if _runner().running() else None
        if project.manifest_path.exists():
            try:
                m = Manifest.load(project.manifest_path)
                if m.source:
                    snap["source"] = {**snap.get("source", {}), **m.source}
                snap["settings"] = m.settings
            except Exception:
                pass
        return jsonify(snap)

    @app.put("/api/projects/<name>/prompt")
    def api_put_prompt(name: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        body = request.get_json(silent=True) or {}
        try:
            content = build_prompt_json(str(body.get("prompt", "")))
        except EditError as exc:
            return _json_error(422, str(exc))
        project.ideas_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(project.prompt_path, content)
        return _snapshot(project)

    @app.put("/api/projects/<name>/settings")
    def api_put_settings(name: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        body = request.get_json(silent=True) or {}
        try:
            manifest = Manifest.load(project.manifest_path)
        except FileNotFoundError:
            return _json_error(404, f"manifest not found for project: {name}")
        if "count" in body:
            count_raw = body["count"]
            if count_raw is None or str(count_raw).strip() == "":
                manifest.settings.pop("count", None)
            else:
                try:
                    count = _validate_count(count_raw)
                except (ValueError, TypeError):
                    return _json_error(422, "count must be an integer >= 1")
                manifest.set_setting("count", count)
        if "desired_length" in body:
            dl_raw = body["desired_length"]
            if dl_raw is None or str(dl_raw).strip() == "":
                manifest.settings.pop("desired_length", None)
            else:
                try:
                    desired_length = int(dl_raw)
                    if desired_length < 10:
                        raise ValueError
                except (ValueError, TypeError):
                    return _json_error(422, "desired_length must be an integer >= 10")
                manifest.set_setting("desired_length", desired_length)
        manifest.save(project.manifest_path)
        return _snapshot(project)

    @app.put("/api/projects/<name>/ideas/<slug>")
    def api_put_idea(name: str, slug: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        idea_path = project.idea_file(slug)
        if not idea_path.exists():
            return _json_error(404, f"no such idea: {slug}")
        body = request.get_json(silent=True) or {}
        try:
            new_text = apply_idea_edit(
                idea_path.read_text(encoding="utf-8"),
                title=str(body.get("title", "")),
                description=str(body.get("description", "")),
                tags=str(body.get("tags", "")),
                narration=str(body.get("narration", "")),
                approved=bool(body.get("approved", False)),
            )
        except EditError as exc:
            return _json_error(422, str(exc))
        _atomic_write(idea_path, new_text)
        return _snapshot(project)

    @app.put("/api/projects/<name>/ideas/<slug>/approved")
    def api_put_approved(name: str, slug: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        idea_path = project.idea_file(slug)
        if not idea_path.exists():
            return _json_error(404, f"no such idea: {slug}")
        body = request.get_json(silent=True) or {}
        try:
            new_text = set_approved(
                idea_path.read_text(encoding="utf-8"), bool(body.get("approved", False))
            )
        except ValueError as exc:
            return _json_error(422, f"unexpected idea file format: {exc}")
        _atomic_write(idea_path, new_text)
        return _snapshot(project)

    @app.post("/api/projects/<name>/ideas/<slug>/refine")
    def api_refine_idea(name: str, slug: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        idea_path = project.idea_file(slug)
        if not idea_path.exists():
            return _json_error(404, f"no such idea: {slug}")

        body = request.get_json(silent=True) or {}
        field = body.get("field")
        if field not in ("title", "description", "tags"):
            return _json_error(422, "field must be one of: title, description, tags")

        if not config.openai_api_key:
            return _json_error(500, "OPENAI_API_KEY is not configured")

        text = idea_path.read_text(encoding="utf-8")
        parsed = parse_idea_file(text)
        hook = _section(text, "Hook")

        current_title = body.get("current_title")
        if current_title is None:
            current_title = parsed.frontmatter.get("title", "") or ""

        current_description = body.get("current_description")
        if current_description is None:
            current_description = parsed.description or ""

        current_tags = body.get("current_tags")
        if current_tags is None:
            current_tags = parsed.tags or ""

        video_title = project.name
        if project.manifest_path.exists():
            try:
                manifest = Manifest.load(project.manifest_path)
                video_title = manifest.source.get("title") or project.name
            except Exception:
                video_title = project.name

        try:
            client = get_client(config.openai_api_key)
            refined = refine_idea_text(
                client,
                field=field,
                user_prompt=str(body.get("prompt", "") or ""),
                current_title=str(current_title),
                current_description=str(current_description),
                current_tags=str(current_tags),
                narration=parsed.narration,
                hook=hook,
                video_title=video_title,
                model=config.ideate.model,
            )
        except Exception as exc:
            return _json_error(500, str(exc))

        return jsonify({"field": field, "result": refined}), 200

    @app.get("/api/projects/<name>/voice/<slug>")
    def api_voice(name: str, slug: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        mp3 = project.voice_file(slug)
        if not mp3.exists():
            return _json_error(404, f"no audio for {slug}")
        return send_from_directory(
            project.voice_dir, mp3.name, mimetype="audio/mpeg", conditional=True
        )

    @app.get("/api/projects/<name>/categories")
    def api_categories(name: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        return jsonify(category_report(project, config))

    @app.put("/api/projects/<name>/ideas/<slug>/plan")
    def api_put_plan(name: str, slug: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        body = request.get_json(silent=True) or {}
        try:
            content = validate_plan_text(str(body.get("plan", "")))
        except EditError as exc:
            return _json_error(422, str(exc))
        project.renders_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(project.plan_file(slug), content)
        return _snapshot(project)

    _RUNNABLE = tuple(s for s in ALLOWED_STAGES if s != "fetch")

    @app.post("/api/projects")
    def api_new_project():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or request.form.get("name") or "").strip()
        if not name:
            return _json_error(400, "name is required")

        force_val = body.get("force") if "force" in body else request.form.get("force")
        force = bool(force_val) if not isinstance(force_val, str) else force_val.lower() in ("true", "1")

        count_raw = body.get("count") if "count" in body else request.form.get("count")
        if count_raw is not None and str(count_raw).strip() != "":
            try:
                count = _validate_count(count_raw)
            except (ValueError, TypeError):
                return _json_error(422, "count must be an integer >= 1")
        else:
            count = None

        url = str(body.get("url") or request.form.get("url") or "").strip()
        text = str(body.get("text") or request.form.get("text") or "").strip()
        uploaded_file = request.files.get("file")

        has_url = bool(url)
        has_text = bool(text)
        has_file = uploaded_file is not None and bool(uploaded_file.filename and uploaded_file.filename.strip())

        if uploaded_file is not None and not (uploaded_file.filename and uploaded_file.filename.strip()):
            if not has_url and not has_text:
                return _json_error(400, "file is required and filename cannot be empty")

        num_sources = sum([has_url, has_text, has_file])
        if num_sources == 0:
            return _json_error(400, "url, text, or file is required")
        if num_sources > 1:
            return _json_error(400, "provide only one of url, text, or file")

        slug = slugify(name)

        if has_url:
            cmd = [sys.executable, "-m", "shorts",
                   *stage_argv("fetch", slug, url=url, force=force, count=count)]
            try:
                _runner().start("fetch", slug, cmd)
            except JobBusy as exc:
                return _json_error(409, str(exc))
            return jsonify(_runner().state()), 202

        if has_text:
            content = text
            source_dict = {"type": "text", "title": name}
        else:  # has_file
            ext = Path(uploaded_file.filename).suffix.lower()
            if ext not in (".txt", ".md"):
                return _json_error(400, "only .txt and .md files are supported")
            content_bytes = uploaded_file.read()
            if len(content_bytes) > 5 * 1024 * 1024:
                return _json_error(400, "file exceeds 5MB limit")
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return _json_error(400, "failed to decode file as UTF-8")
            filename = Path(uploaded_file.filename).name
            if not filename:
                return _json_error(400, "filename cannot be empty")
            source_dict = {"type": "file", "title": name, "file": filename}

        project_root = config.projects_dir / slug
        if project_root.exists():
            if not force:
                return _json_error(409, f"project '{slug}' already exists")
            project = Project.load(config.projects_dir, slug)
        else:
            project = Project.create(config.projects_dir, slug)

        project.ensure_dirs()
        ensure_prompt_file(project)
        project.transcript_txt_path.write_text(content, encoding="utf-8")

        manifest = (
            Manifest.load(project.manifest_path)
            if project.manifest_path.exists()
            else Manifest.new(slug)
        )
        manifest.source = source_dict
        manifest.stage_skipped("fetch")
        manifest.stage_skipped("transcribe")
        if count is not None:
            manifest.set_setting("count", count)
        manifest.save(project.manifest_path)

        snap = _snapshot(project)
        if isinstance(snap, tuple):
            return snap
        return snap, 201

    @app.post("/api/projects/<name>/run/<stage>")
    def api_run_stage(name: str, stage: str):
        if stage not in _RUNNABLE:
            return _json_error(400, f"cannot run stage: {stage}")
        try:
            _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        body = request.get_json(silent=True) or {}
        count = None
        desired_length = None
        if stage == "ideate":
            count_raw = body.get("count")
            if count_raw is not None:
                try:
                    count = _validate_count(count_raw)
                except (ValueError, TypeError):
                    return _json_error(422, "count must be an integer >= 1")
        slugs = body.get("slugs")
        if slugs is not None:
            if not isinstance(slugs, list) or not all(isinstance(s, str) for s in slugs):
                return _json_error(422, "slugs must be a list of strings")

        dl_raw = body.get("desired_length")
        if dl_raw is not None and str(dl_raw).strip() != "":
            try:
                desired_length = int(dl_raw)
                if desired_length < 10:
                    raise ValueError
            except (ValueError, TypeError):
                return _json_error(422, "desired_length must be an integer >= 10")

        cmd = [sys.executable, "-m", "shorts",
               *stage_argv(stage, name, force=bool(body.get("force")), count=count, desired_length=desired_length, slugs=slugs)]
        try:
            _runner().start(stage, name, cmd)
        except JobBusy as exc:
            return _json_error(409, str(exc))
        return jsonify(_runner().state()), 202

    @app.get("/api/jobs/current/stream")
    def api_stream():
        runner = _runner()

        def gen():
            q = runner.attach()
            try:
                while True:
                    try:
                        event = q.get(timeout=_HEARTBEAT_SECONDS)
                    except queue.Empty:
                        yield ": heartbeat\n\n"
                        continue
                    if event is None:
                        return
                    yield sse_format(event)
            finally:
                runner.detach(q)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/<platform>/status")
    def api_platform_status(platform):
        from shorts.publish_target import PublishAuthError
        if platform not in ("youtube", "tiktok"):
            return _json_error(404, f"unknown platform: {platform}")
        target = _target_for(platform, config)
        if not target.is_configured(config):
            return jsonify({"connected": False, "channel": None, "error": "not configured"})
        try:
            creds = target.get_credentials(config)
        except PublishAuthError as exc:
            err = "token expired" if getattr(exc, "reason", "") == "expired" else "not connected"
            return jsonify({"connected": False, "channel": None, "error": err})
        try:
            ch = target.account_label(creds)
        except Exception:
            ch = None
        return jsonify({"connected": True, "channel": ch, "error": None})

    @app.post("/api/<platform>/auth")
    def api_platform_auth(platform):
        if platform not in ("youtube", "tiktok"):
            return _json_error(404, f"unknown platform: {platform}")
        cmd = [sys.executable, "-m", "shorts", *stage_argv(f"{platform}-auth", "")]
        try:
            _runner().start(f"{platform}-auth", "", cmd)
        except JobBusy as exc:
            return _json_error(409, str(exc))
        return jsonify(_runner().state()), 202

    def _platform_from_query():
        platform = request.args.get("platform", "youtube")
        if platform not in ("youtube", "tiktok"):
            return None, _json_error(400, f"unknown platform: {platform}")
        return platform, None

    @app.get("/api/projects/<name>/publish")
    def api_publish_queue(name: str):
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        platform, err = _platform_from_query()
        if err:
            return err
        return jsonify(publish_queue(project, config, platform))

    @app.put("/api/projects/<name>/publish/cadence")
    def api_put_cadence(name: str):
        # function-local: keeps import cost out of routes that don't touch publish/scheduling
        from shorts.publish import parse_iso
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        platform, err = _platform_from_query()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        manifest = Manifest.load(project.manifest_path)
        start = body.get("start")
        if not start:
            manifest.publish = {}
        else:
            try:
                parse_iso(str(start))
            except ValueError:
                return _json_error(422, "start must be an ISO datetime")
            try:
                interval = int(body.get("interval_hours") or 24)
            except (TypeError, ValueError):
                return _json_error(422, "interval_hours must be an integer")
            if interval <= 0:
                return _json_error(422, "interval_hours must be > 0")
            weekdays = body.get("weekdays")
            if weekdays is not None:
                if not isinstance(weekdays, list) or not all(
                    type(d) is int and 1 <= d <= 7 for d in weekdays
                ):
                    return _json_error(422, "weekdays must be integers 1..7")
                weekdays = list(weekdays) or None
            times = body.get("times")
            cleaned_times = None
            if times is not None:
                if not isinstance(times, list):
                    return _json_error(422, "times must be a list of HH:MM strings")
                cleaned_times_set = set()
                for t in times:
                    if not isinstance(t, str):
                        return _json_error(422, "times must be a list of HH:MM strings")
                    m = _TIME_RE.match(t.strip())
                    if not m:
                        return _json_error(422, "times must be a list of HH:MM strings")
                    cleaned_times_set.add(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
                cleaned_times = sorted(cleaned_times_set)

            pub = {
                "start": str(start),
                "interval_hours": interval,
                "weekdays": weekdays,
            }
            if times is not None:
                pub["times"] = cleaned_times
            manifest.publish = pub
        manifest.save(project.manifest_path)
        return jsonify(publish_queue(project, config, platform))

    @app.put("/api/projects/<name>/ideas/<slug>/publish-at")
    def api_put_publish_at(name: str, slug: str):
        # function-local: keeps import cost out of routes that don't touch publish/scheduling
        from shorts.publish import parse_iso
        try:
            project = _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        if not project.idea_file(slug).exists():
            return _json_error(404, f"no such idea: {slug}")
        platform, err = _platform_from_query()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        manifest = Manifest.load(project.manifest_path)
        pa = body.get("publish_at")
        if pa is None:
            entry = manifest.get_idea(slug)
            entry.pop("publish_at", None)
            manifest.ideas[slug] = entry
        else:
            try:
                parse_iso(str(pa))
            except ValueError:
                return _json_error(422, "publish_at must be an ISO datetime")
            manifest.set_idea(slug, publish_at=str(pa))
        manifest.save(project.manifest_path)
        return jsonify(publish_queue(project, config, platform))

    def _start_publish(name, slugs, platform):
        cmd = [sys.executable, "-m", "shorts",
               *stage_argv("publish", name, slugs=slugs, platform=platform)]
        try:
            _runner().start("publish", name, cmd)
        except JobBusy as exc:
            return _json_error(409, str(exc))
        return jsonify(_runner().state()), 202

    @app.post("/api/projects/<name>/publish")
    def api_publish_all(name: str):
        try:
            _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        body = request.get_json(silent=True) or {}
        return _start_publish(name, None, str(body.get("platform") or "youtube"))

    @app.post("/api/projects/<name>/ideas/<slug>/publish")
    def api_publish_one(name: str, slug: str):
        try:
            _load_project(config, name)
        except FileNotFoundError:
            return _json_error(404, f"no such project: {name}")
        body = request.get_json(silent=True) or {}
        return _start_publish(name, [slug], str(body.get("platform") or "youtube"))

    @app.post("/api/jobs/current/cancel")
    def api_cancel():
        try:
            _runner().cancel()
        except JobBusy as exc:
            return _json_error(409, str(exc))
        return jsonify(_runner().state())

    return app
