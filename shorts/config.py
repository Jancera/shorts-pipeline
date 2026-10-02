from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TranscribeCfg:
    model: str


@dataclass(frozen=True)
class IdeateCfg:
    model: str
    desired_video_length: int


@dataclass(frozen=True)
class VoiceCfg:
    base_url: str
    model: str
    voice: str
    language: str | None
    speed: float
    instruct: str | None


@dataclass(frozen=True)
class SubtitleCfg:
    enabled: bool
    font: str | None
    font_size: int | None
    primary_color: str | None
    bold: bool
    italic: bool
    uppercase: bool
    position: str | None
    margin_vertical: int | None
    max_chars_per_line: int | None
    max_lines: int | None
    max_duration: float | None


@dataclass(frozen=True)
class RenderCfg:
    min_beat_duration: float
    video_fit_mode: str
    subtitle: SubtitleCfg


@dataclass(frozen=True)
class YouTubeCfg:
    client_secret: str | None
    token_path: Path
    category_id: int


@dataclass(frozen=True)
class TikTokCfg:
    client_key: str | None
    client_secret: str | None
    token_path: Path
    privacy_level: str
    disable_duet: bool
    disable_stitch: bool
    disable_comment: bool
    is_aigc: bool


@dataclass(frozen=True)
class Config:
    root: Path
    projects_dir: Path
    assets_dir: Path
    aspect: str
    transcribe: TranscribeCfg
    ideate: IdeateCfg
    voice: VoiceCfg
    render: RenderCfg
    openai_api_key: str
    youtube: YouTubeCfg
    tiktok: TikTokCfg


_ASPECTS = {"9:16", "16:9"}
_TIKTOK_PRIVACY = {"SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"}
_SUBTITLE_POSITIONS = {"bottom", "middle", "top"}
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SUBTITLE_STYLING_FIELDS = (
    "font",
    "font_size",
    "primary_color",
    "bold",
    "italic",
    "uppercase",
    "position",
    "margin_vertical",
    "max_chars_per_line",
    "max_lines",
    "max_duration",
)
_FIELD_ALIASES = {
    "size": "font_size",
    "color": "primary_color",
}


def _to_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _pos_int(value: object, key: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be an integer, got {value!r}") from None
    if out < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {out}")
    return out


def _subtitle_cfg(s: dict) -> SubtitleCfg:
    color = s.get("primary_color")
    if color is not None:
        color = str(color)
        if not _HEX_COLOR.match(color):
            raise ConfigError(
                f"render.subtitle.primary_color must be a hex color #RRGGBB, "
                f"got {color!r}"
            )

    position = s.get("position")
    if position is not None:
        position = str(position)
        if position not in _SUBTITLE_POSITIONS:
            raise ConfigError(
                f"render.subtitle.position must be one of "
                f"{sorted(_SUBTITLE_POSITIONS)}, got {position!r}"
            )

    font = s.get("font")

    max_duration = s.get("max_duration")
    if max_duration is not None:
        max_duration = float(max_duration)
        if max_duration <= 0:
            raise ConfigError("render.subtitle.max_duration must be > 0")

    return SubtitleCfg(
        enabled=bool(s.get("enabled", True)),
        font=str(font) if font is not None else None,
        font_size=_pos_int(s.get("font_size"), "render.subtitle.font_size", minimum=1),
        primary_color=color,
        bold=bool(s.get("bold", False)),
        italic=bool(s.get("italic", False)),
        uppercase=bool(s.get("uppercase", False)),
        position=position,
        margin_vertical=_pos_int(
            s.get("margin_vertical"), "render.subtitle.margin_vertical", minimum=0
        ),
        max_chars_per_line=_pos_int(
            s.get("max_chars_per_line"),
            "render.subtitle.max_chars_per_line",
            minimum=1,
        ),
        max_lines=_pos_int(
            s.get("max_lines"), "render.subtitle.max_lines", minimum=1
        ),
        max_duration=max_duration,
    )


def load_config(root: Path | None = None) -> Config:
    root = Path(root or Path.cwd()).resolve()

    cfg_path = root / "config.toml"
    if not cfg_path.is_file():
        raise ConfigError(
            f"config.toml not found at {cfg_path} (copy config.example.toml)"
        )
    try:
        raw = tomllib.loads(cfg_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc

    load_dotenv(root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("OPENAI_API_KEY is not set (put it in .env)")

    if "projects_dir" not in raw:
        raise ConfigError("config.toml missing required key: projects_dir")
    projects_dir = Path(str(raw["projects_dir"])).expanduser()
    if not projects_dir.is_absolute():
        projects_dir = (root / projects_dir).resolve()

    if "assets_dir" not in raw:
        raise ConfigError("config.toml missing required key: assets_dir")
    assets_dir = Path(str(raw["assets_dir"])).expanduser()
    if not assets_dir.is_dir():
        raise ConfigError(f"assets_dir does not exist: {assets_dir}")

    aspect = str(raw.get("aspect", "9:16"))
    if aspect not in _ASPECTS:
        raise ConfigError(
            f"aspect must be one of {sorted(_ASPECTS)}, got {aspect!r}"
        )

    t = raw.get("transcribe", {})
    i = raw.get("ideate", {})
    v = raw.get("voice", {})
    r = raw.get("render", {})

    min_beat = float(r.get("min_beat_duration", 3.0))
    if min_beat <= 0:
        raise ConfigError("render.min_beat_duration must be > 0")

    video_fit_mode = str(r.get("video_fit_mode", "cover"))
    if video_fit_mode not in {"cover", "contain"}:
        raise ConfigError(f"render.video_fit_mode must be 'cover' or 'contain', got {video_fit_mode!r}")

    subtitle = _subtitle_cfg(r.get("subtitle", {}))

    voice_instruct = v.get("instruct")
    if voice_instruct is not None:
        voice_instruct = str(voice_instruct).strip() or None

    voice_speed = float(v.get("speed", 1.0))
    if voice_speed <= 0:
        raise ConfigError("voice.speed must be > 0")

    voice_language = v.get("language")
    if voice_language is not None:
        voice_language = str(voice_language)

    yt = raw.get("youtube", {})
    yt_secret = yt.get("client_secret")
    if yt_secret is not None:
        p = Path(str(yt_secret)).expanduser()
        yt_secret = str(p if p.is_absolute() else (root / p).resolve())
    yt_token = Path(str(yt.get("token_path", ".youtube_token.json"))).expanduser()
    if not yt_token.is_absolute():
        yt_token = (root / yt_token).resolve()
    yt_category = int(yt.get("category_id", 22))
    if yt_category <= 0:
        raise ConfigError("youtube.category_id must be > 0")

    tt = raw.get("tiktok", {})
    tiktok_client_key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip() or None
    tiktok_client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip() or None
    tt_token = Path(str(tt.get("token_path", ".tiktok_token.json"))).expanduser()
    if not tt_token.is_absolute():
        tt_token = (root / tt_token).resolve()
    tt_privacy = str(tt.get("privacy_level", "SELF_ONLY"))
    if tt_privacy not in _TIKTOK_PRIVACY:
        raise ConfigError(f"tiktok.privacy_level must be one of {sorted(_TIKTOK_PRIVACY)}, got {tt_privacy!r}")

    try:
        desired_length = int(i.get("desired_video_length", 90))
    except (ValueError, TypeError):
        raise ConfigError("ideate.desired_video_length must be an integer >= 10") from None
    if desired_length < 10:
        raise ConfigError("ideate.desired_video_length must be >= 10")

    return Config(
        root=root,
        projects_dir=projects_dir,
        assets_dir=assets_dir,
        aspect=aspect,
        transcribe=TranscribeCfg(model=str(t.get("model", "whisper-1"))),
        ideate=IdeateCfg(
            model=str(i.get("model", "gpt-4.1")),
            desired_video_length=desired_length,
        ),
        voice=VoiceCfg(
            base_url=str(v.get("base_url", "http://localhost:3900")),
            model=str(v.get("model", "omnivoice")),
            voice=str(v.get("voice", "default")),
            language=voice_language,
            speed=voice_speed,
            instruct=voice_instruct,
        ),
        render=RenderCfg(
            min_beat_duration=min_beat,
            video_fit_mode=video_fit_mode,
            subtitle=subtitle
        ),
        openai_api_key=api_key,
        youtube=YouTubeCfg(
            client_secret=yt_secret,
            token_path=yt_token,
            category_id=yt_category,
        ),
        tiktok=TikTokCfg(
            client_key=tiktok_client_key,
            client_secret=tiktok_client_secret,
            token_path=tt_token,
            privacy_level=tt_privacy,
            disable_duet=bool(tt.get("disable_duet", False)),
            disable_stitch=bool(tt.get("disable_stitch", False)),
            disable_comment=bool(tt.get("disable_comment", False)),
            is_aigc=bool(tt.get("is_aigc", False)),
        ),
    )


def update_subtitle_config(
    payload: dict,
    root: Path | str | None = None,
    *,
    config_path: Path | str | None = None,
) -> SubtitleCfg:
    """Update `[render.subtitle]` in config.toml from `payload`, preserving comments.

    Keys mapping to SubtitleCfg styling fields that are missing or null in `payload`
    are removed so they revert to engine defaults. Writes changes back to disk
    and returns the resulting SubtitleCfg.
    """
    if not isinstance(payload, dict):
        raise ConfigError(f"payload must be a dict, got {type(payload).__name__}")

    if config_path is not None:
        cfg_path = Path(config_path).expanduser().resolve()
    elif root is not None:
        p = Path(root).expanduser().resolve()
        if p.is_file() or p.suffix == ".toml":
            cfg_path = p
        else:
            cfg_path = p / "config.toml"
    else:
        cfg_path = (Path.cwd() / "config.toml").resolve()

    if not cfg_path.is_file():
        raise ConfigError(
            f"config.toml not found at {cfg_path} (copy config.example.toml)"
        )

    try:
        raw_text = cfg_path.read_text(encoding="utf-8")
        doc = tomlkit.parse(raw_text)
    except Exception as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc

    norm: dict[str, object] = {}
    for k, v in payload.items():
        norm[_FIELD_ALIASES.get(k, k)] = v

    validated_styling: dict[str, object] = {}
    for field in _SUBTITLE_STYLING_FIELDS:
        val = norm.get(field)
        if val is None or val == "":
            continue
        if field == "font":
            font_str = str(val).strip()
            if font_str:
                validated_styling[field] = font_str
        elif field == "font_size":
            validated_styling[field] = _pos_int(
                val, "render.subtitle.font_size", minimum=1
            )
        elif field == "primary_color":
            color = str(val).strip()
            if not _HEX_COLOR.match(color):
                raise ConfigError(
                    f"render.subtitle.primary_color must be a hex color #RRGGBB, "
                    f"got {color!r}"
                )
            validated_styling[field] = color
        elif field in ("bold", "italic", "uppercase"):
            validated_styling[field] = _to_bool(val)
        elif field == "position":
            pos = str(val).strip().lower()
            if pos not in _SUBTITLE_POSITIONS:
                raise ConfigError(
                    f"render.subtitle.position must be one of "
                    f"{sorted(_SUBTITLE_POSITIONS)}, got {pos!r}"
                )
            validated_styling[field] = pos
        elif field == "margin_vertical":
            validated_styling[field] = _pos_int(
                val, "render.subtitle.margin_vertical", minimum=0
            )
        elif field == "max_chars_per_line":
            validated_styling[field] = _pos_int(
                val, "render.subtitle.max_chars_per_line", minimum=1
            )
        elif field == "max_lines":
            validated_styling[field] = _pos_int(
                val, "render.subtitle.max_lines", minimum=1
            )
        elif field == "max_duration":
            try:
                dur = float(val)
            except (TypeError, ValueError):
                raise ConfigError(
                    f"render.subtitle.max_duration must be a number, got {val!r}"
                ) from None
            if dur <= 0:
                raise ConfigError("render.subtitle.max_duration must be > 0")
            validated_styling[field] = dur

    if "render" not in doc:
        doc["render"] = tomlkit.table()
    render_tbl = doc["render"]
    if not isinstance(render_tbl, MutableMapping):
        raise ConfigError("render in config.toml must be a table")

    if "subtitle" not in render_tbl:
        render_tbl["subtitle"] = tomlkit.table()
    sub_table = render_tbl["subtitle"]
    if not isinstance(sub_table, MutableMapping):
        raise ConfigError("render.subtitle in config.toml must be a table")

    if "enabled" in norm:
        val = norm["enabled"]
        if val is None or val == "":
            sub_table.pop("enabled", None)
        else:
            sub_table["enabled"] = _to_bool(val)

    for field in _SUBTITLE_STYLING_FIELDS:
        if field in validated_styling:
            sub_table[field] = validated_styling[field]
        else:
            sub_table.pop(field, None)

    content = tomlkit.dumps(doc)
    tmp_path: Path | None = None
    try:
        stat = os.stat(cfg_path) if cfg_path.exists() else None
        with tempfile.NamedTemporaryFile(
            "w",
            dir=cfg_path.parent,
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        if stat is not None:
            os.chmod(tmp_path, stat.st_mode)
        tmp_path.replace(cfg_path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return _subtitle_cfg(dict(sub_table))

