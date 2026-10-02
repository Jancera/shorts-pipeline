import json
import types
from pathlib import Path

import pytest

from shorts.youtube import SCOPES, YouTubeAuthError, get_credentials


class _Cfg:
    def __init__(self, token_path):
        self.youtube = types.SimpleNamespace(
            token_path=token_path, client_secret=None, category_id=22
        )


def _valid_token(path):
    path.write_text(json.dumps({
        "token": "at", "refresh_token": "rt", "client_id": "cid",
        "client_secret": "csec", "scopes": SCOPES,
        "token_uri": "https://oauth2.googleapis.com/token",
    }))


def test_scopes_exact():
    assert SCOPES == ["https://www.googleapis.com/auth/youtube.upload"]


def test_get_credentials_missing_file_raises(tmp_path):
    with pytest.raises(YouTubeAuthError) as ei:
        get_credentials(_Cfg(tmp_path / "nope.json"))
    assert ei.value.reason == "missing"


def test_get_credentials_valid_returned_as_is(tmp_path, monkeypatch):
    tok = tmp_path / "tok.json"
    _valid_token(tok)

    class FakeCreds:
        valid = True
        expired = False
        refresh_token = "rt"
        def to_json(self): return "{}"

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        staticmethod(lambda *a, **k: FakeCreds()),
    )
    creds = get_credentials(_Cfg(tok))
    assert isinstance(creds, FakeCreds)


def test_get_credentials_refreshes_and_rewrites(tmp_path, monkeypatch):
    tok = tmp_path / "tok.json"
    _valid_token(tok)
    calls = {"refresh": 0}

    class FakeCreds:
        valid = False
        expired = True
        refresh_token = "rt"
        def refresh(self, _request): calls["refresh"] += 1
        def to_json(self): return '{"token": "new"}'

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        staticmethod(lambda *a, **k: FakeCreds()),
    )
    get_credentials(_Cfg(tok))
    assert calls["refresh"] == 1
    assert tok.read_text() == '{"token": "new"}'


def test_get_credentials_refresh_error_is_expired(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    tok = tmp_path / "tok.json"
    _valid_token(tok)

    class FakeCreds:
        valid = False
        expired = True
        refresh_token = "rt"
        def refresh(self, _request): raise RefreshError("bad")
        def to_json(self): return "{}"

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        staticmethod(lambda *a, **k: FakeCreds()),
    )
    with pytest.raises(YouTubeAuthError) as ei:
        get_credentials(_Cfg(tok))
    assert ei.value.reason == "expired"


class _FakeResp:
    def __init__(self, status): self.status = status


class _FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = _FakeResp(status)


class _FakeRequest:
    """next_chunk() yields (None, response) after `fail_times` transient errors."""
    def __init__(self, fail_times=0, status=503):
        self.fail_times = fail_times
        self.status = status
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _FakeHttpError(self.status)
        return (None, {"id": "vid123"})


class _FakeService:
    def __init__(self, request): self._request = request
    def videos(self): return self
    def insert(self, **_kw): return self._request


def test_insert_video_success(tmp_path, monkeypatch):
    import shorts.youtube as yt
    monkeypatch.setattr(yt, "MediaFileUpload", None, raising=False)
    # patch the lazy imports inside insert_video
    monkeypatch.setattr(
        "googleapiclient.http.MediaFileUpload",
        lambda *a, **k: object(), raising=False,
    )
    monkeypatch.setattr("googleapiclient.errors.HttpError", _FakeHttpError, raising=False)
    mp4 = tmp_path / "v.mp4"; mp4.write_bytes(b"x")
    res = yt.insert_video(_FakeService(_FakeRequest()), mp4_path=mp4, body={"snippet": {}})
    assert res == {"video_id": "vid123", "url": "https://youtu.be/vid123"}


def test_insert_video_retries_transient(tmp_path, monkeypatch):
    import shorts.youtube as yt
    monkeypatch.setattr(
        "googleapiclient.http.MediaFileUpload", lambda *a, **k: object(), raising=False,
    )
    monkeypatch.setattr("googleapiclient.errors.HttpError", _FakeHttpError, raising=False)
    monkeypatch.setattr(yt.time, "sleep", lambda _s: None)
    mp4 = tmp_path / "v.mp4"; mp4.write_bytes(b"x")
    req = _FakeRequest(fail_times=2, status=503)
    res = yt.insert_video(_FakeService(req), mp4_path=mp4, body={"snippet": {}})
    assert res["video_id"] == "vid123"
    assert req.calls == 3


class _FlakyConnRequest:
    """next_chunk() raises ConnectionResetError `fail_times` times, then succeeds."""
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionResetError("connection reset by peer")
        return (None, {"id": "vid123"})


def test_insert_video_retries_connection_error(tmp_path, monkeypatch):
    import shorts.youtube as yt
    monkeypatch.setattr(
        "googleapiclient.http.MediaFileUpload", lambda *a, **k: object(), raising=False,
    )
    monkeypatch.setattr("googleapiclient.errors.HttpError", _FakeHttpError, raising=False)
    monkeypatch.setattr(yt.time, "sleep", lambda _s: None)
    mp4 = tmp_path / "v.mp4"; mp4.write_bytes(b"x")
    req = _FlakyConnRequest(fail_times=2)
    res = yt.insert_video(_FakeService(req), mp4_path=mp4, body={"snippet": {}})
    assert res["video_id"] == "vid123"
    assert req.calls == 3


def test_insert_video_connection_error_exhausts_retries_and_reraises(tmp_path, monkeypatch):
    import shorts.youtube as yt
    monkeypatch.setattr(
        "googleapiclient.http.MediaFileUpload", lambda *a, **k: object(), raising=False,
    )
    monkeypatch.setattr("googleapiclient.errors.HttpError", _FakeHttpError, raising=False)
    monkeypatch.setattr(yt.time, "sleep", lambda _s: None)
    mp4 = tmp_path / "v.mp4"; mp4.write_bytes(b"x")
    with pytest.raises(ConnectionResetError):
        yt.insert_video(_FakeService(_FlakyConnRequest(fail_times=99)),
                        mp4_path=mp4, body={"snippet": {}}, max_retries=3)


def test_insert_video_non_transient_reraises(tmp_path, monkeypatch):
    import shorts.youtube as yt
    monkeypatch.setattr(
        "googleapiclient.http.MediaFileUpload", lambda *a, **k: object(), raising=False,
    )
    monkeypatch.setattr("googleapiclient.errors.HttpError", _FakeHttpError, raising=False)
    mp4 = tmp_path / "v.mp4"; mp4.write_bytes(b"x")
    with pytest.raises(_FakeHttpError):
        yt.insert_video(_FakeService(_FakeRequest(fail_times=1, status=400)),
                        mp4_path=mp4, body={"snippet": {}})


def test_upload_for_target_merges_privacy_and_title(tmp_path, monkeypatch):
    import shorts.youtube as yt

    body = {
        "snippet": {"title": "My Title", "description": "d", "tags": [], "categoryId": "22"},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    monkeypatch.setattr(
        yt, "insert_video",
        lambda client, *, mp4_path, body: {"video_id": "v1", "url": "u1"},
    )
    result = yt._upload_for_target(object(), mp4_path=Path("x.mp4"), body=body)
    assert result == {
        "video_id": "v1", "url": "u1",
        "privacy": body["status"]["privacyStatus"],
        "title": body["snippet"]["title"],
    }


def _http_error(status: int, payload: dict):
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = ""

    return HttpError(_Resp(status), json.dumps(payload).encode())


def test_parse_upload_error_quota_aborts_batch():
    import shorts.youtube as yt

    exc = _http_error(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})
    assert yt._parse_upload_error(exc) == {
        "message": "403 quotaExceeded", "abort_batch": True,
    }


def test_parse_upload_error_non_quota_http_error_does_not_abort():
    import shorts.youtube as yt

    exc = _http_error(500, {"error": {"message": "Internal error"}})
    result = yt._parse_upload_error(exc)
    assert result["abort_batch"] is False
    assert result["message"] == "500 Internal error"


def test_parse_upload_error_generic_exception():
    import shorts.youtube as yt

    assert yt._parse_upload_error(RuntimeError("boom")) == {
        "message": "boom", "abort_batch": False,
    }


def test_target_returns_configured_publish_target(tmp_path):
    import shorts.youtube as yt

    config = types.SimpleNamespace(
        youtube=types.SimpleNamespace(
            client_secret=None,
            token_path=tmp_path / ".youtube_token.json",
            category_id=22,
        )
    )
    t = yt.target(config)
    assert t.key == "youtube"
    assert t.label == "YouTube"
    assert t.supports_scheduling is True
    got = t.build_body(title="t", description="d", tags="a,b", publish_at=None)
    expected = yt.build_video_body(
        title="t", description="d", tags="a,b",
        category_id=config.youtube.category_id, publish_at=None,
    )
    assert got == expected
