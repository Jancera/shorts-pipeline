import os
import textwrap
from pathlib import Path

import pytest

from shorts.config import load_config, ConfigError, update_subtitle_config


def _toml_path(p: Path) -> str:
    """TOML string literals treat backslashes as escapes, so Windows paths
    (C:\\Users\\...) must use forward slashes when embedded in TOML."""
    return p.as_posix()


def _write(root: Path, *, toml: str | None = None, env_key: str | None = "sk-test",
           make_assets: bool = True) -> Path:
    assets = root / "assets"
    if make_assets:
        assets.mkdir()
    default_toml = textwrap.dedent(f"""
        projects_dir = "projects"
        assets_dir = "{_toml_path(assets)}"
        aspect = "9:16"

        [transcribe]
        model = "whisper-1"

        [ideate]
        model = "gpt-4.1"

        [voice]
        model = "omnivoice"
        voice = "39f10351"

        [render]
        min_beat_duration = 3.0
    """)
    (root / "config.toml").write_text(toml if toml is not None else default_toml)
    env_lines = []
    if env_key is not None:
        env_lines.append(f"OPENAI_API_KEY={env_key}")
    (root / ".env").write_text("\n".join(env_lines) + ("\n" if env_lines else ""))
    return root


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SHORTS_IDEATE_COUNT", raising=False)


def _write_env(tmp_path, *, openai_key: str = "sk-test"):
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={openai_key}\n")


def test_loads_valid_config(tmp_path):
    _write(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.projects_dir == (tmp_path / "projects").resolve()
    assert cfg.assets_dir == (tmp_path / "assets")
    assert cfg.aspect == "9:16"
    assert cfg.ideate.desired_video_length == 90
    assert cfg.voice.base_url == "http://localhost:3900"
    assert cfg.voice.model == "omnivoice"
    assert cfg.voice.voice == "39f10351"
    assert cfg.voice.language is None
    assert cfg.voice.speed == 1.0
    assert cfg.voice.instruct is None
    assert cfg.render.min_beat_duration == 3.0
    assert cfg.render.subtitle.enabled is True  # default when table absent
    assert cfg.render.subtitle.font is None
    assert cfg.render.subtitle.bold is False
    assert cfg.render.subtitle.position is None
    assert cfg.openai_api_key == "sk-test"


def _write_render_subtitle(tmp_path, body: str):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n'
        f"[render.subtitle]\n{body}"
    )
    _write_env(tmp_path)


def test_render_subtitles_can_be_disabled(tmp_path):
    _write_render_subtitle(tmp_path, "enabled = false\n")
    assert load_config(tmp_path).render.subtitle.enabled is False


def test_render_subtitle_full_customization(tmp_path):
    _write_render_subtitle(tmp_path, (
        'font = "Montserrat"\n'
        "font_size = 18\n"
        'primary_color = "#FFCC00"\n'
        "bold = true\n"
        "italic = true\n"
        "uppercase = true\n"
        'position = "top"\n'
        "margin_vertical = 40\n"
        "max_chars_per_line = 32\n"
        "max_lines = 2\n"
        "max_duration = 4.0\n"
    ))
    sub = load_config(tmp_path).render.subtitle
    assert sub.enabled is True
    assert sub.font == "Montserrat"
    assert sub.font_size == 18
    assert sub.primary_color == "#FFCC00"
    assert (sub.bold, sub.italic, sub.uppercase) == (True, True, True)
    assert sub.position == "top"
    assert sub.margin_vertical == 40
    assert sub.max_chars_per_line == 32
    assert sub.max_lines == 2
    assert sub.max_duration == 4.0


def test_render_subtitle_bad_position(tmp_path):
    _write_render_subtitle(tmp_path, 'position = "sideways"\n')
    with pytest.raises(ConfigError, match="render.subtitle.position must be one of"):
        load_config(tmp_path)


def test_render_subtitle_bad_color(tmp_path):
    _write_render_subtitle(tmp_path, 'primary_color = "yellow"\n')
    with pytest.raises(ConfigError, match="render.subtitle.primary_color"):
        load_config(tmp_path)


def test_render_subtitle_bad_font_size(tmp_path):
    _write_render_subtitle(tmp_path, "font_size = 0\n")
    with pytest.raises(ConfigError, match="render.subtitle.font_size must be >= 1"):
        load_config(tmp_path)


def test_voice_full_customization(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n'
        "[voice]\n"
        'base_url = "http://localhost:4000"\n'
        'model = "voxcpm2"\n'
        'voice = "demo0001"\n'
        'language = "pt"\n'
        "speed = 1.25\n"
        'instruct = "  calm, warm  "\n'
    )
    _write_env(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.voice.base_url == "http://localhost:4000"
    assert cfg.voice.model == "voxcpm2"
    assert cfg.voice.voice == "demo0001"
    assert cfg.voice.language == "pt"
    assert cfg.voice.speed == 1.25
    assert cfg.voice.instruct == "calm, warm"


def test_voice_blank_instruct_becomes_none(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n'
        '[voice]\ninstruct = "   "\n'
    )
    _write_env(tmp_path)
    assert load_config(tmp_path).voice.instruct is None


def test_voice_bad_speed(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n'
        "[voice]\nspeed = 0\n"
    )
    _write_env(tmp_path)
    with pytest.raises(ConfigError, match="voice.speed must be > 0"):
        load_config(tmp_path)


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="config.toml not found"):
        load_config(tmp_path)


def test_missing_api_key(tmp_path):
    _write(tmp_path, env_key=None)
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(tmp_path)


def test_missing_assets_dir(tmp_path):
    _write(tmp_path, make_assets=False)
    with pytest.raises(ConfigError, match="assets_dir does not exist"):
        load_config(tmp_path)


def test_bad_aspect(tmp_path):
    _write(tmp_path, toml='projects_dir="projects"\nassets_dir="%s"\naspect="1:1"\n'
           % _toml_path(tmp_path / "assets"))
    with pytest.raises(ConfigError, match="aspect must be one of"):
        load_config(tmp_path)


def test_invalid_toml(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text("this is = = not toml")
    _write_env(tmp_path)
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(tmp_path)


def test_youtube_defaults_when_section_absent(tmp_path):
    _write(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.youtube.client_secret is None
    assert cfg.youtube.token_path == (tmp_path / ".youtube_token.json").resolve()
    assert cfg.youtube.category_id == 22


def test_youtube_section_parsed_and_resolved(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n'
        '[youtube]\nclient_secret = "creds/cs.json"\n'
        'token_path = "sub/tok.json"\ncategory_id = 20\n'
    )
    _write_env(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.youtube.client_secret == str((tmp_path / "creds/cs.json").resolve())
    assert cfg.youtube.token_path == (tmp_path / "sub/tok.json").resolve()
    assert cfg.youtube.category_id == 20


def test_youtube_bad_category_id(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n[youtube]\ncategory_id = 0\n'
    )
    _write_env(tmp_path)
    with pytest.raises(ConfigError, match="youtube.category_id must be > 0"):
        load_config(tmp_path)


def test_tiktok_defaults_when_section_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
    _write(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.tiktok.client_key is None
    assert cfg.tiktok.client_secret is None
    assert cfg.tiktok.token_path == (tmp_path / ".tiktok_token.json").resolve()
    assert cfg.tiktok.privacy_level == "SELF_ONLY"
    assert cfg.tiktok.is_aigc is False
    assert cfg.tiktok.disable_duet is False
    assert cfg.tiktok.disable_stitch is False
    assert cfg.tiktok.disable_comment is False


def test_tiktok_section_parsed_and_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck-123")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs-456")
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n'
        '[tiktok]\ntoken_path = "sub/tt.json"\n'
        'privacy_level = "PUBLIC_TO_EVERYONE"\n'
        "disable_duet = true\n"
        "is_aigc = true\n"
    )
    _write_env(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.tiktok.client_key == "ck-123"
    assert cfg.tiktok.client_secret == "cs-456"
    assert cfg.tiktok.token_path == (tmp_path / "sub/tt.json").resolve()
    assert cfg.tiktok.privacy_level == "PUBLIC_TO_EVERYONE"
    assert cfg.tiktok.disable_duet is True
    assert cfg.tiktok.disable_stitch is False
    assert cfg.tiktok.disable_comment is False
    assert cfg.tiktok.is_aigc is True


def test_tiktok_bad_privacy_level(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "config.toml").write_text(
        f'projects_dir="projects"\nassets_dir="{_toml_path(assets)}"\n\n'
        '[tiktok]\nprivacy_level = "PUBLIC"\n'
    )
    _write_env(tmp_path)
    with pytest.raises(ConfigError, match="tiktok.privacy_level must be one of"):
        load_config(tmp_path)


def test_update_subtitle_config_updates_and_preserves_comments(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    initial_toml = textwrap.dedent(f"""\
        # Top-level global configuration
        projects_dir = "projects"
        assets_dir = "{_toml_path(assets)}"
        aspect = "9:16"

        [render]
        # Render section configuration
        min_beat_duration = 3.0

        [render.subtitle]
        # Subtitle styling configuration
        enabled = true
        # Default font name
        font = "Montserrat"
        font_size = 18
        primary_color = "#FFFFFF" # Hex primary color
    """)
    (tmp_path / "config.toml").write_text(initial_toml)
    _write_env(tmp_path)

    res = update_subtitle_config({
        "font": "Impact",
        "size": 24,
        "primary_color": "#FFCC00",
    }, root=tmp_path)

    assert res.font == "Impact"
    assert res.font_size == 24
    assert res.primary_color == "#FFCC00"

    cfg = load_config(tmp_path)
    assert cfg.render.subtitle.font == "Impact"
    assert cfg.render.subtitle.font_size == 24
    assert cfg.render.subtitle.primary_color == "#FFCC00"

    content = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "# Top-level global configuration" in content
    assert "# Render section configuration" in content
    assert "# Subtitle styling configuration" in content
    assert "# Default font name" in content
    assert "# Hex primary color" in content


def test_update_subtitle_config_reverts_missing_and_null_keys(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    initial_toml = textwrap.dedent(f"""\
        # Global comment
        projects_dir = "projects"
        assets_dir = "{_toml_path(assets)}"
        aspect = "9:16"

        [render.subtitle]
        # Subtitle defaults
        enabled = true
        font = "Montserrat"
        font_size = 18
        primary_color = "#FFFFFF"
        bold = true
        italic = true
        uppercase = true
        position = "top"
        margin_vertical = 40
        max_chars_per_line = 32
        max_lines = 2
        max_duration = 4.0
    """)
    (tmp_path / "config.toml").write_text(initial_toml)
    _write_env(tmp_path)

    # font is updated, primary_color is explicitly null, all other styling keys omitted
    update_subtitle_config({
        "font": "Open Sans",
        "primary_color": None,
    }, root=tmp_path)

    cfg = load_config(tmp_path)
    sub = cfg.render.subtitle
    assert sub.font == "Open Sans"
    assert sub.font_size is None
    assert sub.primary_color is None
    assert sub.bold is False
    assert sub.italic is False
    assert sub.uppercase is False
    assert sub.position is None
    assert sub.margin_vertical is None
    assert sub.max_chars_per_line is None
    assert sub.max_lines is None
    assert sub.max_duration is None

    content = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "# Global comment" in content
    assert "# Subtitle defaults" in content
    assert 'font = "Open Sans"' in content
    assert "font_size" not in content
    assert "primary_color" not in content
    assert "bold" not in content
    assert "italic" not in content
    assert "uppercase" not in content
    assert "position" not in content
    assert "margin_vertical" not in content
    assert "max_chars_per_line" not in content
    assert "max_lines" not in content
    assert "max_duration" not in content


def test_update_subtitle_config_toggle_enabled(tmp_path):
    _write_render_subtitle(tmp_path, "enabled = true\nfont = \"Arial\"\n")
    update_subtitle_config({"enabled": False, "font": "Arial"}, root=tmp_path)
    assert load_config(tmp_path).render.subtitle.enabled is False

    update_subtitle_config({"enabled": True, "font": "Arial"}, root=tmp_path)
    assert load_config(tmp_path).render.subtitle.enabled is True


def test_update_subtitle_config_empty_strings_revert_keys(tmp_path):
    _write_render_subtitle(tmp_path, "font = \"Arial\"\nfont_size = 20\n")
    update_subtitle_config({"font": "", "font_size": ""}, root=tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.render.subtitle.font is None
    assert cfg.render.subtitle.font_size is None


def test_update_subtitle_config_creates_tables_when_missing(tmp_path):
    _write(tmp_path)  # does not have [render.subtitle]
    update_subtitle_config({"font": "Courier New", "size": 16}, root=tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.render.subtitle.font == "Courier New"
    assert cfg.render.subtitle.font_size == 16


def test_update_subtitle_config_direct_file_path(tmp_path):
    cfg_file = tmp_path / "custom_config.toml"
    cfg_file.write_text("# Preserved note\n[render.subtitle]\nfont = 'Montserrat'\n")
    update_subtitle_config({"font": "Verdana"}, cfg_file)
    content = cfg_file.read_text(encoding="utf-8")
    assert 'font = "Verdana"' in content
    assert "# Preserved note" in content


def test_update_subtitle_config_validation_errors(tmp_path):
    _write_render_subtitle(tmp_path, "enabled = true\n")

    with pytest.raises(ConfigError, match="payload must be a dict"):
        update_subtitle_config("not a dict", root=tmp_path)  # type: ignore

    with pytest.raises(ConfigError, match="render.subtitle.position must be one of"):
        update_subtitle_config({"position": "sideways"}, root=tmp_path)

    with pytest.raises(ConfigError, match="render.subtitle.primary_color"):
        update_subtitle_config({"color": "invalid_hex"}, root=tmp_path)

    with pytest.raises(ConfigError, match="render.subtitle.font_size must be >= 1"):
        update_subtitle_config({"size": 0}, root=tmp_path)

    with pytest.raises(ConfigError, match="render.subtitle.margin_vertical must be >= 0"):
        update_subtitle_config({"margin_vertical": -1}, root=tmp_path)

    with pytest.raises(ConfigError, match="render.subtitle.max_chars_per_line must be >= 1"):
        update_subtitle_config({"max_chars_per_line": 0}, root=tmp_path)

    with pytest.raises(ConfigError, match="render.subtitle.max_lines must be >= 1"):
        update_subtitle_config({"max_lines": 0}, root=tmp_path)

    with pytest.raises(ConfigError, match="render.subtitle.max_duration must be > 0"):
        update_subtitle_config({"max_duration": -1.0}, root=tmp_path)


def test_update_subtitle_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="config.toml not found"):
        update_subtitle_config({"font": "Arial"}, root=tmp_path)


def test_update_subtitle_config_atomic_write_failure_cleans_up(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    initial_content = "[render.subtitle]\nfont = 'Montserrat'\n"
    cfg_file.write_text(initial_content, encoding="utf-8")
    _write_env(tmp_path)

    def mock_replace(self, target):
        raise OSError("Disk failure during replace")

    monkeypatch.setattr(Path, "replace", mock_replace)

    with pytest.raises(OSError, match="Disk failure during replace"):
        update_subtitle_config({"font": "Impact"}, root=tmp_path)

    assert cfg_file.read_text(encoding="utf-8") == initial_content
    remaining_files = [p.name for p in tmp_path.iterdir() if p.name != ".env"]
    assert remaining_files == ["config.toml"]


def test_update_subtitle_config_preserves_file_permissions(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[render.subtitle]\nfont = 'Montserrat'\n", encoding="utf-8")
    _write_env(tmp_path)
    os.chmod(cfg_file, 0o644)

    update_subtitle_config({"font": "Impact"}, root=tmp_path)

    assert os.stat(cfg_file).st_mode & 0o777 == 0o644




