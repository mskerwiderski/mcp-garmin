"""The admin side: invitations and accounts, all from the command line."""
import pytest

from garmin_mcp import users
from garmin_mcp.cli import main


def test_invite_create_prints_a_signup_link(capsys, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.garmin.example")
    assert main(["invite", "create", "--label", "Anja"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("https://mcp.garmin.example/signup?code=")
    code = out.split("code=")[1].split()[0]
    assert users.invite_valid(code)


def test_invite_list_shows_the_state(capsys, user_id):
    main(["invite", "list"])
    assert "used" in capsys.readouterr().out


def test_user_list(capsys, user_id):
    assert main(["user", "list"]) == 0
    out = capsys.readouterr().out
    assert "anja@example.com" in out and "not connected" in out


def test_disable_and_enable(capsys, user_id):
    assert main(["user", "disable", "anja@example.com"]) == 0
    with pytest.raises(users.UserError, match="disabled"):
        users.verify_login("anja@example.com", "supersecret123")
    assert main(["user", "enable", "anja@example.com"]) == 0
    assert users.verify_login("anja@example.com", "supersecret123") == user_id


def test_delete_needs_confirmation(capsys, user_id, isolated_home):
    assert main(["user", "delete", "anja@example.com"]) == 1
    assert users.get_user(user_id) is not None

    cache = isolated_home / "cache" / str(user_id)
    cache.mkdir(parents=True)
    (cache / "1.fit").write_bytes(b"x")

    assert main(["user", "delete", "anja@example.com", "--yes"]) == 0
    assert users.get_user(user_id) is None
    assert not cache.exists()


def test_unknown_account(capsys):
    assert main(["user", "disable", "nobody@example.com"]) == 1
