from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from shorts.publish_target import PublishAuthError, PublishConfigError, PublishTarget

_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = ["video.publish"]
_HINT = "run: python -m shorts tiktok auth"


class TikTokAuthError(PublishAuthError):
    pass


class TikTokConfigError(PublishConfigError):
    pass


class TikTokUploadError(Exception):
    def __init__(self, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _load_token(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_token(path: Path, token: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token))
    os.chmod(path, 0o600)


def _token_from_response(data: dict) -> dict:
    if "access_token" not in data:
        raise TikTokAuthError(
            f"TikTok token request failed: "
            f"{data.get('error_description') or data.get('error') or data!r}",
            reason="expired",
        )
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": time.time() + int(data.get("expires_in", 86400)),
        "open_id": data.get("open_id"),
    }


def get_credentials(config) -> dict:
    path: Path = config.tiktok.token_path
    token = _load_token(path)
    if token is None:
        raise TikTokAuthError(f"no TikTok token - {_HINT}", reason="missing")
    if time.time() < token.get("expires_at", 0) - 60:
        return token
    if not token.get("refresh_token"):
        raise TikTokAuthError(f"TikTok token unusable - {_HINT}", reason="missing")
    try:
        token = _refresh(config, token["refresh_token"])
    except (urllib.error.URLError, TikTokConfigError) as exc:
        raise TikTokAuthError(f"TikTok token expired - {_HINT}", reason="expired") from exc
    _save_token(path, token)
    return token


def _refresh(config, refresh_token: str) -> dict:
    if not config.tiktok.client_key or not config.tiktok.client_secret:
        raise TikTokConfigError("set TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env")
    payload = urllib.parse.urlencode({
        "client_key": config.tiktok.client_key,
        "client_secret": config.tiktok.client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return _token_from_response(data)


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize(config):
    if not config.tiktok.client_key or not config.tiktok.client_secret:
        raise TikTokConfigError("set TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if qs.get("state", [""])[0] == state and "code" in qs:
                result["code"] = qs["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"TikTok auth complete - you can close this tab.")
            done.set()
        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.handle_request, daemon=True).start()

    auth_url = _AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": config.tiktok.client_key,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print(f"open this URL to authorize TikTok (or it may open automatically):\n{auth_url}")
    try:
        import webbrowser
        webbrowser.open(auth_url)
    except Exception:
        pass

    done.wait(timeout=300)
    if "code" not in result:
        raise TikTokAuthError("TikTok authorization timed out or was denied", reason="missing")

    payload = urllib.parse.urlencode({
        "client_key": config.tiktok.client_key,
        "client_secret": config.tiktok.client_secret,
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    token = _token_from_response(data)
    _save_token(config.tiktok.token_path, token)
    return token


def account_label(token: dict) -> str:
    return token.get("open_id", "") or ""


def build_client(token: dict) -> dict:
    return token


def _post_json(url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def creator_info(token: dict) -> dict:
    return _post_json(_CREATOR_INFO_URL, token["access_token"])["data"]


def build_post_info(
    *, title: str, description: str, tags: str, publish_at=None,
    privacy_level: str, disable_duet: bool, disable_stitch: bool,
    disable_comment: bool, is_aigc: bool,
) -> dict:
    hashtags = " ".join(f"#{t.strip().replace(' ', '')}" for t in tags.split(",") if t.strip())
    caption = f"{title}\n\n{description}".strip()
    if hashtags:
        caption = f"{caption}\n\n{hashtags}"
    return {
        "post_info": {
            "title": caption[:2200],
            "privacy_level": privacy_level,
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
            "disable_comment": disable_comment,
            "is_aigc": is_aigc,
        },
    }


def upload_video(client: dict, *, mp4_path: Path, body: dict, config) -> dict:
    token = client["access_token"]
    size = mp4_path.stat().st_size

    info = creator_info(client)
    allowed = set(info.get("privacy_level_options", []))
    wanted = body["post_info"]["privacy_level"]
    if not allowed:
        raise TikTokUploadError(
            "TikTok creator_info returned no privacy_level_options - cannot verify "
            "the account is allowed to post with the configured privacy_level",
            code="privacy_level_unknown",
        )
    if wanted not in allowed:
        raise TikTokUploadError(
            f"privacy_level {wanted!r} not allowed for this account "
            f"(allowed: {sorted(allowed)}) - unaudited apps are usually SELF_ONLY-only",
            code="privacy_level_not_allowed",
        )

    init_payload = {**body, "source_info": {
        "source": "FILE_UPLOAD", "video_size": size,
        "chunk_size": size, "total_chunk_count": 1,
    }}
    init = _post_json(_INIT_URL, token, init_payload)
    err = init.get("error") or {}
    if err.get("code") not in (None, "ok"):
        raise TikTokUploadError(err.get("message", "init failed"), code=err.get("code", ""))
    publish_id = init["data"]["publish_id"]
    upload_url = init["data"]["upload_url"]

    put_req = urllib.request.Request(upload_url, data=mp4_path.read_bytes(), method="PUT", headers={
        "Content-Type": "video/mp4", "Content-Length": str(size),
        "Content-Range": f"bytes 0-{size - 1}/{size}",
    })
    with urllib.request.urlopen(put_req, timeout=300):
        pass

    status = _post_json(_STATUS_URL, token, {"publish_id": publish_id})
    return {"publish_id": publish_id, "status": status.get("data", {}).get("status")}


def _parse_upload_error(exc: Exception) -> dict:
    if isinstance(exc, TikTokUploadError):
        abort = exc.code in ("privacy_level_not_allowed", "privacy_level_unknown") or exc.status == 429
        return {"message": str(exc), "abort_batch": abort}
    return {"message": str(exc), "abort_batch": False}


def target(config) -> PublishTarget:
    from functools import partial
    return PublishTarget(
        key="tiktok", label="TikTok", supports_scheduling=False,
        is_configured=lambda c: bool(c.tiktok.client_key and c.tiktok.client_secret),
        get_credentials=get_credentials, authorize=authorize, account_label=account_label,
        build_client=build_client,
        build_body=partial(
            build_post_info,
            privacy_level=config.tiktok.privacy_level,
            disable_duet=config.tiktok.disable_duet,
            disable_stitch=config.tiktok.disable_stitch,
            disable_comment=config.tiktok.disable_comment,
            is_aigc=config.tiktok.is_aigc,
        ),
        upload=lambda client, *, mp4_path, body: upload_video(client, mp4_path=mp4_path, body=body, config=config),
        parse_upload_error=_parse_upload_error,
    )
