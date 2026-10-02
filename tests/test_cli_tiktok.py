from click.testing import CliRunner

import shorts.cli as cli


def _no_config(monkeypatch):
    # bypass load_config; the commands only need cfg.tiktok for these paths
    import types
    fake = types.SimpleNamespace(tiktok=types.SimpleNamespace(
        client_key=None, client_secret=None, token_path=None,
        privacy_level="SELF_ONLY", disable_duet=False, disable_stitch=False,
        disable_comment=False, is_aigc=False))
    monkeypatch.setattr(cli, "_config", lambda ctx: fake)


def test_tiktok_status_connected(monkeypatch):
    _no_config(monkeypatch)
    import shorts.tiktok as tt
    monkeypatch.setattr(tt, "get_credentials", lambda c: object())
    monkeypatch.setattr(tt, "account_label", lambda c: "My TikTok")
    r = CliRunner().invoke(cli.cli, ["tiktok", "status"])
    assert r.exit_code == 0
    assert "connected: My TikTok" in r.output


def test_tiktok_status_needs_auth(monkeypatch):
    _no_config(monkeypatch)
    import shorts.tiktok as tt
    from shorts.publish_target import PublishAuthError

    def boom(c):
        raise PublishAuthError("TikTok token expired - run: python -m shorts tiktok auth",
                                reason="expired")
    monkeypatch.setattr(tt, "get_credentials", boom)
    r = CliRunner().invoke(cli.cli, ["tiktok", "status"])
    assert r.exit_code == 0
    assert "run: python -m shorts tiktok auth" in r.output


def test_publish_platform_tiktok_selects_target(monkeypatch):
    _no_config(monkeypatch)
    monkeypatch.setattr(cli, "_resolve", lambda cfg, name: f"project:{name}")
    import shorts.publish as pub
    seen = {}
    monkeypatch.setattr(pub, "run",
        lambda project, config, target, *, slugs, force: seen.update(
            project=project, target=target, slugs=slugs, force=force))
    r = CliRunner().invoke(cli.cli, ["publish", "demo", "--platform", "tiktok"])
    assert r.exit_code == 0
    assert seen["target"].key == "tiktok"
