import pytest

from garmin_mcp import crypto, users


def test_invite_is_single_use(user_id):
    code = users.create_invite("Bob")
    assert users.invite_valid(code)
    users.create_user("bob@example.com", "anotherlongpw", code)
    assert not users.invite_valid(code)
    with pytest.raises(users.UserError, match="invalid"):
        users.create_user("eve@example.com", "yetanotherpw", code)


def test_expired_invite_is_refused():
    code = users.create_invite("Old", ttl_ms=-1)
    assert not users.invite_valid(code)


def test_no_account_without_an_invite():
    with pytest.raises(users.UserError):
        users.create_user("eve@example.com", "longenoughpw", "made-up-code")


def test_duplicate_email_and_weak_password(user_id):
    with pytest.raises(users.UserError, match="already exists"):
        users.create_user("anja@example.com", "supersecret123",
                          users.create_invite())
    with pytest.raises(users.UserError, match="10 characters"):
        users.create_user("new@example.com", "short", users.create_invite())


def test_login_locks_out_after_five_attempts(user_id):
    for _ in range(5):
        with pytest.raises(users.UserError, match="wrong"):
            users.verify_login("anja@example.com", "nope")
    with pytest.raises(users.UserError, match="Too many"):
        users.verify_login("anja@example.com", "supersecret123")


def test_successful_login_clears_the_counter(user_id):
    for _ in range(4):
        with pytest.raises(users.UserError):
            users.verify_login("anja@example.com", "nope")
    assert users.verify_login("anja@example.com", "supersecret123") == user_id
    with pytest.raises(users.UserError, match="wrong"):
        users.verify_login("anja@example.com", "nope")     # counter restarted


def test_sessions(user_id):
    token = users.start_session(user_id)
    assert users.session_user(token) == user_id
    users.end_session(token)
    assert users.session_user(token) is None
    assert users.session_user(None) is None


def test_disabling_a_user_cuts_every_session(user_id):
    token = users.start_session(user_id)
    users.set_status(user_id, "disabled")
    assert users.session_user(token) is None
    with pytest.raises(users.UserError, match="disabled"):
        users.verify_login("anja@example.com", "supersecret123")


def test_garmin_tokens_are_encrypted_at_rest(user_id, monkeypatch):
    users.set_garmin_tokens(user_id, '{"oauth_token": "t1"}', '{"access_token": "a"}',
                            "Anja Muster")
    from garmin_mcp.db import conn
    with conn() as c:
        raw = c.execute("SELECT oauth1_enc FROM garmin_tokens WHERE user_id=?",
                        (user_id,)).fetchone()["oauth1_enc"]
    assert "oauth_token" not in raw and raw.startswith("gAAAA")
    assert users.get_garmin_tokens(user_id)[2] == "Anja Muster"

    monkeypatch.setenv(crypto.ENV_SECRET, "a-different-secret")
    assert users.get_garmin_tokens(user_id) is None      # unreadable, not a crash


def test_delete_user_removes_everything(user_id):
    users.set_garmin_tokens(user_id, "{}", "{}", "x")
    users.start_session(user_id)
    users.delete_user(user_id)
    assert users.get_user(user_id) is None
    assert users.get_garmin_tokens(user_id) is None
    assert users.list_users() == []


def test_missing_app_secret_is_a_clear_error(monkeypatch):
    monkeypatch.delenv(crypto.ENV_SECRET, raising=False)
    with pytest.raises(crypto.MissingSecret, match="APP_SECRET"):
        crypto.encrypt("x")
