import dataclasses

import pytest

from shorts.publish_target import PublishAuthError, PublishConfigError, PublishTarget


def test_publish_auth_error_with_custom_reason():
    """PublishAuthError stores reason parameter."""
    exc = PublishAuthError("no token", reason="expired")
    assert str(exc) == "no token"
    assert exc.reason == "expired"


def test_publish_auth_error_default_reason():
    """PublishAuthError defaults reason to 'missing' when not provided."""
    exc = PublishAuthError("no token")
    assert str(exc) == "no token"
    assert exc.reason == "missing"


def test_publish_config_error():
    """PublishConfigError behaves like a plain Exception."""
    exc = PublishConfigError("not set up")
    assert str(exc) == "not set up"
    assert not hasattr(exc, "reason")


def test_publish_target_fields():
    """PublishTarget dataclass has all required fields accessible."""
    target = PublishTarget(
        key="fake",
        label="Fake",
        supports_scheduling=True,
        is_configured=lambda c: True,
        get_credentials=lambda c: "creds",
        authorize=lambda c: "auth_result",
        account_label=lambda c: "My Account",
        build_client=lambda c: "client_obj",
        build_body=lambda **kwargs: {"key": "value"},
        upload=lambda **kwargs: {"upload": "result"},
        parse_upload_error=lambda exc: {"message": "error", "abort_batch": False},
    )

    assert target.key == "fake"
    assert target.label == "Fake"
    assert target.supports_scheduling is True
    assert target.is_configured("config") is True
    assert target.get_credentials("config") == "creds"
    assert target.authorize("config") == "auth_result"
    assert target.account_label("config") == "My Account"
    assert target.build_client("config") == "client_obj"
    assert target.build_body(test=1) == {"key": "value"}
    assert target.upload(test=1) == {"upload": "result"}
    assert target.parse_upload_error(Exception("test")) == {"message": "error", "abort_batch": False}


def test_publish_target_is_frozen():
    """PublishTarget is a frozen dataclass."""
    target = PublishTarget(
        key="fake",
        label="Fake",
        supports_scheduling=True,
        is_configured=lambda c: True,
        get_credentials=lambda c: "creds",
        authorize=lambda c: "auth_result",
        account_label=lambda c: "My Account",
        build_client=lambda c: "client_obj",
        build_body=lambda **kwargs: {"key": "value"},
        upload=lambda **kwargs: {"upload": "result"},
        parse_upload_error=lambda exc: {"message": "error", "abort_batch": False},
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        target.key = "other"
