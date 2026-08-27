"""The admin page: invitations, accounts, and who may see it at all."""
import pytest
from starlette.testclient import TestClient

from garmin_mcp import users
from garmin_mcp.cli import main
from garmin_mcp.server import build_http_app


@pytest.fixture
def client():
    with TestClient(build_http_app(), base_url="https://testserver") as c:
        yield c


def _login(client, email="anja@example.com", password="supersecret123"):
    r = client.post("/login", data={"email": email, "password": password})
    assert r.status_code == 200
    return r


def _second_account(email="bob@example.com"):
    return users.create_user(email, "anotherlongpw", users.create_invite("Bob"))


def test_the_first_account_administers_the_server(user_id):
    assert users.is_admin(user_id)
    assert not users.is_admin(_second_account())


def test_admin_page_is_invisible_to_normal_users(client, user_id):
    _second_account()
    _login(client, "bob@example.com", "anotherlongpw")
    r = client.get("/admin")
    assert r.status_code == 404          # not 403: no hint that it exists
    assert "Administration" not in r.text


def test_admin_page_needs_a_login(client, user_id):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


def test_account_page_links_admin_only_for_admins(client, user_id):
    assert '/admin' in _login(client).text
    _second_account()
    client.get("/logout")
    assert '/admin' not in _login(client, "bob@example.com", "anotherlongpw").text


def test_creating_an_invitation_shows_a_usable_link(client, user_id, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.garmin.example")
    _login(client)
    r = client.post("/admin/invite", data={"label": "Anja"})
    assert r.status_code == 200 and "Invitation created" in r.text
    code = r.text.split("signup?code=")[1].split("<")[0]
    assert users.invite_valid(code)
    assert "https://mcp.garmin.example/signup?code=" in r.text
    assert "Anja" in client.get("/admin").text        # listed as an invitation


def test_the_invitation_comes_with_a_ready_made_mail(client, user_id, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.garmin.example")
    _login(client)
    page = client.post("/admin/invite", data={"label": "Anja"}).text
    code = page.split("signup?code=")[1].split("<")[0]

    # Both texts carry the working link and the connector URL, and say what
    # happens to the Garmin password - the question every tester asks.
    for marker in ("Mail text (English)", "Mailtext (deutsch)"):
        assert marker in page
    assert page.count(f"signup?code={code}") >= 3          # link, EN, DE
    assert "https://mcp.garmin.example/mcp" in page
    assert "never stored" in page and "nicht gespeichert" in page
    assert "expires in 7 days" in page and "7 Tagen ab" in page

    # And a prefilled mail draft for the client that opens one.
    assert "mailto:?subject=Your%20access" in page
    assert 'onclick="copyMail(' in page


def test_no_mail_text_before_an_invitation_exists(client, user_id):
    _login(client)
    page = client.get("/admin").text
    assert "Mail text (English)" not in page and "mailto:" not in page


def test_the_mail_text_is_escaped_not_injected(client, user_id):
    """The link goes into a textarea and into a mailto href - both need
    escaping, or a crafted PUBLIC_URL would break out."""
    from garmin_mcp.web import invitation_mail
    subject, body = invitation_mail(
        "https://x.example/signup?code=a&b=<script>", "https://x.example")
    assert "<script>" in body                       # raw text keeps it
    page_html = client.post("/admin/invite", data={"label": "x"}).text
    assert "<script>alert" not in page_html


def test_accounts_and_their_garmin_state_are_listed(client, user_id):
    other = _second_account()
    users.set_garmin_tokens(other, "{}", "{}", "Bob Garmin")
    _login(client)
    page = client.get("/admin").text
    assert "anja@example.com" in page and "that is you" in page
    assert "bob@example.com" in page and "Bob Garmin" in page


def test_disable_and_enable_from_the_page(client, user_id):
    other = _second_account()
    _login(client)
    client.post(f"/admin/user/{other}/disable")
    assert users.get_user(other)["status"] == "disabled"
    client.post(f"/admin/user/{other}/enable")
    assert users.get_user(other)["status"] == "active"


def test_delete_from_the_page_removes_everything(client, user_id, isolated_home):
    other = _second_account()
    users.set_garmin_tokens(other, "{}", "{}", "Bob")
    cache = isolated_home / "cache" / str(other)
    cache.mkdir(parents=True)
    (cache / "1.fit").write_bytes(b"x")

    _login(client)
    client.post(f"/admin/user/{other}/delete")
    assert users.get_user(other) is None
    assert users.get_garmin_tokens(other) is None
    assert not cache.exists()


def test_an_admin_cannot_lock_themselves_out_here(client, user_id):
    _login(client)
    r = client.post(f"/admin/user/{user_id}/delete")
    assert r.status_code == 400 and "your own account" in r.text
    assert users.get_user(user_id) is not None


def test_unknown_account(client, user_id):
    _login(client)
    assert client.post("/admin/user/9999/disable").status_code == 404


def test_cli_can_promote_and_refuses_the_last_demotion(capsys, user_id):
    other = _second_account()
    assert main(["user", "promote", "bob@example.com"]) == 0
    assert users.is_admin(other)
    assert main(["user", "demote", "bob@example.com"]) == 0
    assert main(["user", "demote", "anja@example.com"]) == 1     # last one standing
    assert users.is_admin(user_id)


def test_migration_promotes_the_oldest_account_of_an_older_database(user_id):
    """A database from before the admin page has no admin - the person who set
    the server up is the oldest account."""
    from garmin_mcp import db
    _second_account()
    with db.conn() as c:
        c.execute("UPDATE users SET is_admin=0")
    db.init()
    assert users.is_admin(user_id)
