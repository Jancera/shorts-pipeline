import base64
import hashlib
import json
import time
import types
import urllib.error
from datetime import datetime

import pytest

import shorts.tiktok as tiktok
from shorts.tiktok import TikTokAuthError, TikTokUploadError, get_credentials


def _cfg(tmp_path, **overrides):
    fields = dict(
        client_key="ck",
        client_secret="cs",
        token_path=tmp_path / "tiktok_token.json",
        privacy_level="SELF_ONLY",
        disable_duet=False,
        disable_stitch=False,
        disable_comment=False,
        is_aigc=False,
    )
    fields.update(overrides)
    return types.SimpleNamespace(tiktok=types.SimpleNamespace(**fields))


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_pkce_pair_produces_valid_s256_challenge():
    verifier, challenge = tiktok._pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert "=" not in verifier and "=" not in challenge


def test_get_credentials_missing_file_raises(tmp_path):
    with pytest.raises(TikTokAuthError) as ei:
        get_credentials(_cfg(tmp_path, token_path=tmp_path / "nope.json"))
    assert ei.value.reason == "missing"


def test_get_credentials_valid_unexpired_returns_as_is(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    token = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": time.time() + 86400,
        "open_id": "oid",
    }
    cfg.tiktok.token_path.write_text(json.dumps(token))

    def _boom(*_a, **_k):
        raise AssertionError("network call made for a still-valid token")

    monkeypatch.setattr("shorts.tiktok.urllib.request.urlopen", _boom)
    got = get_credentials(cfg)
    assert got["access_token"] == "at"
    assert got["open_id"] == "oid"


def test_get_credentials_refreshes_when_expired(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.tiktok.token_path.write_text(json.dumps({
        "access_token": "old",
        "refresh_token": "old-refresh",
        "expires_at": time.time() - 10,
    }))
    payload = json.dumps({
        "access_token": "new",
        "refresh_token": "new-refresh",
        "expires_in": 86400,
    }).encode()
    monkeypatch.setattr(
        "shorts.tiktok.urllib.request.urlopen",
        lambda *_a, **_k: _FakeResp(payload),
    )

    got = get_credentials(cfg)
    assert got["access_token"] == "new"
    assert got["refresh_token"] == "new-refresh"
    assert got["expires_at"] > time.time()

    on_disk = json.loads(cfg.tiktok.token_path.read_text())
    assert on_disk["access_token"] == "new"
    assert on_disk["refresh_token"] == "new-refresh"


def test_get_credentials_refresh_failure_raises_expired(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.tiktok.token_path.write_text(json.dumps({
        "access_token": "old",
        "refresh_token": "old-refresh",
        "expires_at": time.time() - 10,
    }))

    def _fail(*_a, **_k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("shorts.tiktok.urllib.request.urlopen", _fail)
    with pytest.raises(TikTokAuthError) as ei:
        get_credentials(cfg)
    assert ei.value.reason == "expired"


def test_get_credentials_refresh_error_shape_raises_clear_auth_error(tmp_path, monkeypatch):
    """TikTok's token endpoint can return HTTP 200 with an error-shaped body
    (no access_token) for a revoked/expired refresh token. This must surface
    as a clear TikTokAuthError, not a raw KeyError."""
    cfg = _cfg(tmp_path)
    cfg.tiktok.token_path.write_text(json.dumps({
        "access_token": "old",
        "refresh_token": "old-refresh",
        "expires_at": time.time() - 10,
    }))
    payload = json.dumps({
        "error": "invalid_grant",
        "error_description": "refresh token expired",
    }).encode()
    monkeypatch.setattr(
        "shorts.tiktok.urllib.request.urlopen",
        lambda *_a, **_k: _FakeResp(payload),
    )

    with pytest.raises(TikTokAuthError) as ei:
        get_credentials(cfg)
    assert ei.value.reason == "expired"
    assert "refresh token expired" in str(ei.value)


def test_build_post_info_truncates_and_folds_tags():
    kwargs = dict(
        title="x" * 2300,
        description="d",
        tags="one, two",
        privacy_level="SELF_ONLY",
        disable_duet=False,
        disable_stitch=False,
        disable_comment=False,
        is_aigc=False,
    )
    result = tiktok.build_post_info(**kwargs)
    assert len(result["post_info"]["title"]) <= 2200
    assert result["post_info"]["privacy_level"] == "SELF_ONLY"

    short = tiktok.build_post_info(**{**kwargs, "title": "t"})
    caption = short["post_info"]["title"]
    assert "#one" in caption and "#two" in caption

    with_publish_at = tiktok.build_post_info(**{**kwargs, "publish_at": datetime.now()})
    assert with_publish_at == result
    assert "publish_at" not in json.dumps(with_publish_at)


class _PostJsonSpy:
    """Records (url, token, body) and returns canned responses per URL."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, token, body=None):
        self.calls.append({"url": url, "token": token, "body": body})
        return self.responses[url]

    def bodies_for(self, url):
        return [c["body"] for c in self.calls if c["url"] == url]


def _wire_upload(monkeypatch, spy):
    monkeypatch.setattr(tiktok, "_post_json", spy)
    puts = []

    def _fake_urlopen(req, *_a, **_k):
        puts.append(req)
        return _FakeResp(b"")

    monkeypatch.setattr("shorts.tiktok.urllib.request.urlopen", _fake_urlopen)
    return puts


def test_upload_video_sends_correct_init_payload(tmp_path, monkeypatch):
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\x00" * 1234)
    spy = _PostJsonSpy({
        tiktok._CREATOR_INFO_URL: {"data": {"privacy_level_options": ["SELF_ONLY"]}},
        tiktok._INIT_URL: {
            "data": {"publish_id": "p1", "upload_url": "https://example.test/up"},
            "error": None,
        },
        tiktok._STATUS_URL: {"data": {"status": "PROCESSING_UPLOAD"}},
    })
    puts = _wire_upload(monkeypatch, spy)

    body = tiktok.build_post_info(
        title="t", description="d", tags="", privacy_level="SELF_ONLY",
        disable_duet=False, disable_stitch=False, disable_comment=False, is_aigc=False,
    )
    result = tiktok.upload_video(
        {"access_token": "tok"}, mp4_path=mp4, body=body, config=_cfg(tmp_path)
    )

    assert result == {"publish_id": "p1", "status": "PROCESSING_UPLOAD"}

    init_bodies = spy.bodies_for(tiktok._INIT_URL)
    assert len(init_bodies) == 1
    source_info = init_bodies[0]["source_info"]
    assert source_info["source"] == "FILE_UPLOAD"
    assert source_info["video_size"] == mp4.stat().st_size == 1234
    assert source_info["chunk_size"] == 1234
    assert source_info["total_chunk_count"] == 1
    assert init_bodies[0]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert len(puts) == 1


def test_upload_video_rejects_disallowed_privacy_level(tmp_path, monkeypatch):
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\x00" * 16)
    spy = _PostJsonSpy({
        tiktok._CREATOR_INFO_URL: {"data": {"privacy_level_options": ["SELF_ONLY"]}},
        tiktok._INIT_URL: {
            "data": {"publish_id": "p1", "upload_url": "https://example.test/up"},
            "error": None,
        },
        tiktok._STATUS_URL: {"data": {"status": "PROCESSING_UPLOAD"}},
    })
    puts = _wire_upload(monkeypatch, spy)

    body = tiktok.build_post_info(
        title="t", description="d", tags="", privacy_level="PUBLIC_TO_EVERYONE",
        disable_duet=False, disable_stitch=False, disable_comment=False, is_aigc=False,
    )
    with pytest.raises(TikTokUploadError) as ei:
        tiktok.upload_video(
            {"access_token": "tok"}, mp4_path=mp4, body=body, config=_cfg(tmp_path)
        )

    msg = str(ei.value)
    assert "PUBLIC_TO_EVERYONE" in msg and "SELF_ONLY" in msg
    assert ei.value.code == "privacy_level_not_allowed"
    assert spy.bodies_for(tiktok._INIT_URL) == []
    assert puts == []


def test_upload_video_allows_privacy_level_the_account_permits(tmp_path, monkeypatch):
    """Complement to the rejection test: proves the guard discriminates on the
    creator_info options rather than blocking (or passing) unconditionally."""
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\x00" * 16)
    spy = _PostJsonSpy({
        tiktok._CREATOR_INFO_URL: {
            "data": {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]}
        },
        tiktok._INIT_URL: {
            "data": {"publish_id": "p2", "upload_url": "https://example.test/up"},
            "error": None,
        },
        tiktok._STATUS_URL: {"data": {"status": "PROCESSING_UPLOAD"}},
    })
    _wire_upload(monkeypatch, spy)

    body = tiktok.build_post_info(
        title="t", description="d", tags="", privacy_level="PUBLIC_TO_EVERYONE",
        disable_duet=False, disable_stitch=False, disable_comment=False, is_aigc=False,
    )
    result = tiktok.upload_video(
        {"access_token": "tok"}, mp4_path=mp4, body=body, config=_cfg(tmp_path)
    )
    assert result["publish_id"] == "p2"
    assert len(spy.bodies_for(tiktok._INIT_URL)) == 1


def test_upload_video_rejects_when_privacy_level_options_unknown(tmp_path, monkeypatch):
    """creator_info returning no privacy_level_options (empty/missing key) must
    fail closed - never silently let the upload attempt through."""
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\x00" * 16)
    spy = _PostJsonSpy({
        tiktok._CREATOR_INFO_URL: {"data": {}},
        tiktok._INIT_URL: {
            "data": {"publish_id": "p1", "upload_url": "https://example.test/up"},
            "error": None,
        },
        tiktok._STATUS_URL: {"data": {"status": "PROCESSING_UPLOAD"}},
    })
    puts = _wire_upload(monkeypatch, spy)

    body = tiktok.build_post_info(
        title="t", description="d", tags="", privacy_level="SELF_ONLY",
        disable_duet=False, disable_stitch=False, disable_comment=False, is_aigc=False,
    )
    with pytest.raises(TikTokUploadError) as ei:
        tiktok.upload_video(
            {"access_token": "tok"}, mp4_path=mp4, body=body, config=_cfg(tmp_path)
        )

    assert ei.value.code == "privacy_level_unknown"
    assert spy.bodies_for(tiktok._INIT_URL) == []
    assert puts == []


def test_parse_upload_error_privacy_level_aborts_batch():
    exc = TikTokUploadError("x", code="privacy_level_not_allowed")
    assert tiktok._parse_upload_error(exc)["abort_batch"] is True
    assert tiktok._parse_upload_error(TikTokUploadError("y", code="privacy_level_unknown"))["abort_batch"] is True
    assert tiktok._parse_upload_error(TikTokUploadError("y", code="spam"))["abort_batch"] is False
    assert tiktok._parse_upload_error(RuntimeError("boom")) == {
        "message": "boom", "abort_batch": False,
    }


def test_target_returns_tiktok_shaped_publish_target(tmp_path):
    config = _cfg(tmp_path)
    t = tiktok.target(config)
    assert t.key == "tiktok"
    assert t.label == "TikTok"
    assert t.supports_scheduling is False
    assert t.is_configured(config) is True
    body = t.build_body(title="t", description="d", tags="", publish_at=None)
    assert body["post_info"]["privacy_level"] == config.tiktok.privacy_level
    assert body["post_info"]["is_aigc"] is config.tiktok.is_aigc
