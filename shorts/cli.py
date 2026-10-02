from __future__ import annotations

from pathlib import Path

import click

from shorts.config import Config, ConfigError, load_config
from shorts.ideas import sync_idea_state
from shorts.project import Manifest, Project, slugify
from shorts.prompt import ensure_prompt_file
from shorts.publish_target import PublishAuthError, PublishConfigError
from shorts.stages import fetch as fetch_stage
from shorts.stages import ideate as ideate_stage
from shorts.stages import plan as plan_stage
from shorts.stages.plan import plan_opts_hash, subtitle_plan_args
from shorts.stages import render as render_stage
from shorts.stages import transcribe as transcribe_stage
from shorts.stages import voice as voice_stage


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Turn a YouTube video into approved, narrated shorts."""
    ctx.ensure_object(dict)


def _config(ctx: click.Context) -> Config:
    cache = ctx.ensure_object(dict)
    if "config" not in cache:
        try:
            cache["config"] = load_config()
        except ConfigError as exc:
            raise click.ClickException(str(exc))
    return cache["config"]


def _resolve(config: Config, name: str | None) -> Project:
    try:
        return Project.discover(config.projects_dir, name)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))


@cli.command()
@click.argument("name")
@click.option("--text", default=None, help="Plain text instruction or script content.")
@click.option("--file", default=None, type=click.Path(path_type=Path), help="Path to .txt or .md file.")
@click.option("--force", is_flag=True, help="Overwrite existing project if it exists.")
@click.option(
    "--count",
    type=click.IntRange(min=1),
    default=None,
    help="Target number of shorts to generate.",
)
@click.pass_context
def init(
    ctx: click.Context,
    name: str,
    text: str | None,
    file: Path | None,
    force: bool,
    count: int | None,
) -> None:
    """Initialize a new project from text or a file."""
    if (text is None and file is None) or (text is not None and file is not None):
        raise click.ClickException("Provide either --text or --file, but not both.")

    if file is not None:
        if file.suffix.lower() not in (".txt", ".md"):
            raise click.ClickException("Only .txt and .md files are supported.")
        if not file.exists():
            raise click.ClickException(f"File not found: {file}")
        try:
            content = file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise click.ClickException(f"Failed to read file as UTF-8: {exc}")
    else:
        content = text

    config = _config(ctx)
    resolved_name = slugify(name)
    project_root = config.projects_dir / resolved_name
    if project_root.exists():
        if not force:
            raise click.ClickException(
                f"Project '{resolved_name}' already exists. Use --force to overwrite."
            )
        project = Project.load(config.projects_dir, resolved_name)
    else:
        project = Project.create(config.projects_dir, resolved_name)

    project.ensure_dirs()
    ensure_prompt_file(project)
    project.transcript_txt_path.write_text(content, encoding="utf-8")

    manifest = (
        Manifest.load(project.manifest_path)
        if project.manifest_path.exists()
        else Manifest.new(resolved_name)
    )
    manifest.source = {
        "type": "file" if file else "text",
        "title": name,
    }
    if file:
        manifest.source["file"] = str(Path(file).resolve())
    manifest.stage_skipped("fetch")
    manifest.stage_skipped("transcribe")
    if count is not None:
        manifest.set_setting("count", count)
    manifest.save(project.manifest_path)

    click.echo(f"init: initialized project '{resolved_name}' -> {project.root}")


@cli.command()
@click.argument("url")
@click.option("--name", default=None, help="Project name (default: from the video title).")
@click.option("--force", is_flag=True, help="Re-download even if the video exists.")
@click.option(
    "--count",
    type=click.IntRange(min=1),
    default=None,
    help="Target number of shorts to generate.",
)
@click.pass_context
def fetch(
    ctx: click.Context,
    url: str,
    name: str | None,
    force: bool,
    count: int | None,
) -> None:
    """Download a YouTube video as a new project's source."""
    config = _config(ctx)
    resolved_name = slugify(name or fetch_stage.probe_title(url))
    project_root = config.projects_dir / resolved_name
    if project_root.exists():
        project = Project.load(config.projects_dir, resolved_name)
    else:
        project = Project.create(config.projects_dir, resolved_name)
    if not project.manifest_path.exists():
        Manifest.new(resolved_name).save(project.manifest_path)
    if count is not None:
        manifest = Manifest.load(project.manifest_path)
        manifest.set_setting("count", count)
        manifest.save(project.manifest_path)
    fetch_stage.run(project, config, url=url, force=force)


@cli.command()
@click.argument("name", required=False)
@click.option("--force", is_flag=True)
@click.pass_context
def transcribe(ctx: click.Context, name: str | None, force: bool) -> None:
    """Extract audio and transcribe it with Whisper."""
    config = _config(ctx)
    transcribe_stage.run(_resolve(config, name), config, force=force)


@cli.command()
@click.argument("name", required=False)
@click.option("--force", is_flag=True)
@click.option(
    "--count",
    type=click.IntRange(min=1),
    default=None,
    help="Number of shorts to generate (overrides project setting).",
)
@click.option(
    "--desired-length",
    type=click.IntRange(min=10),
    default=None,
    help="Desired video length in seconds (overrides config).",
)
@click.pass_context
def ideate(
    ctx: click.Context,
    name: str | None,
    force: bool,
    count: int | None,
    desired_length: int | None,
) -> None:
    """Generate shorts ideas from the transcript."""
    config = _config(ctx)
    ideate_stage.run(_resolve(config, name), config, force=force, count=count, desired_length=desired_length)


@cli.command()
@click.argument("name", required=False)
@click.option("--force", is_flag=True)
@click.option("--slug", "slugs", multiple=True, help="Synthesize only these ideas.")
@click.pass_context
def voice(ctx: click.Context, name: str | None, force: bool, slugs: tuple[str, ...]) -> None:
    """Synthesize narration for approved ideas."""
    config = _config(ctx)
    voice_stage.run(_resolve(config, name), config, force=force, slugs=list(slugs) or None)


@cli.command()
@click.argument("name", required=False)
@click.option("--force", is_flag=True)
@click.pass_context
def plan(ctx: click.Context, name: str | None, force: bool) -> None:
    """Build ai-vedit plans for approved ideas, for review before render."""
    config = _config(ctx)
    plan_stage.run(_resolve(config, name), config, force=force)


@cli.command()
@click.argument("name", required=False)
@click.option("--force", is_flag=True)
@click.pass_context
def render(ctx: click.Context, name: str | None, force: bool) -> None:
    """Render videos from the reviewed plans via ai-vedit."""
    config = _config(ctx)
    render_stage.run(_resolve(config, name), config, force=force)


@cli.command()
@click.argument("name", required=False)
@click.pass_context
def status(ctx: click.Context, name: str | None) -> None:
    """Show pipeline state for a project."""
    config = _config(ctx)
    project = _resolve(config, name)
    manifest = Manifest.load(project.manifest_path)
    click.echo(f"project: {project.name}  ({project.root})")
    click.echo(
        f"source:  {manifest.source.get('title', '?')}  "
        f"{manifest.source.get('url', '')}"
    )
    for stage in ("fetch", "transcribe", "ideate"):
        entry = manifest.get_stage(stage) or {}
        click.echo(
            f"  {stage:<11} {entry.get('status', '-'):<8} {entry.get('at', '')}"
        )
    if project.ideas_dir.is_dir():
        sync_idea_state(project, manifest)
        manifest.save(project.manifest_path)
        from shorts.web.state import idea_freshness
        opts_hash = plan_opts_hash(
            min_beat_duration=config.render.min_beat_duration,
            subtitle_args=subtitle_plan_args(config.render.subtitle),
        )
        click.echo("ideas:")
        for slug in sorted(manifest.ideas):
            entry = manifest.ideas[slug]
            approved = "x" if entry.get("approved") else " "
            fr = idea_freshness(project, slug, manifest, opts_hash)
            click.echo(
                f"  [{approved}] {slug:<28} "
                f"{'voice' if fr['voice'] == 'fresh' else '-':<6} "
                f"{'plan' if fr['plan'] == 'fresh' else '-':<5} "
                f"{'render' if fr['render'] == 'fresh' else '-'}"
            )


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address (localhost only; do not expose).")
@click.option("--port", default=8765, type=int)
@click.option("--open/--no-open", "open_browser", default=True, help="Open a browser tab.")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int, open_browser: bool) -> None:
    """Run the local web UI (localhost only - do not expose)."""
    config = _config(ctx)
    from shorts.web.app import create_app

    app = create_app(config)
    url = f"http://{host}:{port}"
    click.echo(f"shorts web UI: {url}  (localhost only - do not expose)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    app.run(host=host, port=port, threaded=True, debug=False)


def _register_target_commands(group, target_factory) -> None:
    @group.command("auth")
    @click.pass_context
    def auth_cmd(ctx: click.Context) -> None:
        """One-time browser consent for uploads."""
        config = _config(ctx)
        target = target_factory(config)
        try:
            creds = target.authorize(config)
        except PublishConfigError as exc:
            raise click.ClickException(str(exc))
        try:
            name = target.account_label(creds) or "(no account name)"
        except Exception:
            name = "(account name unavailable)"
        click.echo(f"connected: {name}")

    @group.command("status")
    @click.pass_context
    def status_cmd(ctx: click.Context) -> None:
        """Show whether a usable token is present."""
        config = _config(ctx)
        target = target_factory(config)
        try:
            creds = target.get_credentials(config)
        except PublishAuthError as exc:
            click.echo(str(exc))
            return
        try:
            name = target.account_label(creds) or "(no account name)"
        except Exception:
            name = "(account name unavailable)"
        click.echo(f"connected: {name}")


def _youtube_target(config):
    from shorts.youtube import target
    return target(config)


def _tiktok_target(config):
    from shorts.tiktok import target
    return target(config)


@cli.group()
def youtube() -> None:
    """YouTube auth and status."""


_register_target_commands(youtube, _youtube_target)


@cli.group()
def tiktok() -> None:
    """TikTok auth and status."""


_register_target_commands(tiktok, _tiktok_target)


@cli.command()
@click.argument("name", required=False)
@click.option("--platform", "platform_name", type=click.Choice(["youtube", "tiktok"]),
              default="youtube", help="Which platform to upload to.")
@click.option("--slug", "slugs", multiple=True, help="Publish only these ideas.")
@click.option("--force", is_flag=True, help="Re-upload even if already uploaded (new video).")
@click.pass_context
def publish(
    ctx: click.Context, name: str | None, platform_name: str,
    slugs: tuple[str, ...], force: bool,
) -> None:
    """Upload approved + rendered shorts to the chosen platform."""
    config = _config(ctx)
    from shorts import publish as publish_mod

    target_factory = _youtube_target if platform_name == "youtube" else _tiktok_target
    target = target_factory(config)
    publish_mod.run(_resolve(config, name), config, target, slugs=list(slugs) or None, force=force)


def main() -> None:
    cli()
