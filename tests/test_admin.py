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
    # The address must stand on its own line in both texts, not trail off the
    # end of a sentence - "which URL exactly?" is the question that follows.
    assert page.count("    https://mcp.garmin.example/mcp") >= 2
    # Wrapping may break any phrase across lines, so compare without it.
    flat = " ".join(page.split())
    assert "including the /mcp at the end" in flat
    assert "mit dem /mcp am Ende" in flat
    assert "no API key, no token, no port" in flat
    assert "never stored" in flat and "nicht gespeichert" in flat
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


def test_open_invitations_can_be_revoked_from_the_page(client, user_id, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.garmin.example")
    _login(client)
    code = users.create_invite("Anja")
    page = client.get("/admin").text
    # An open invitation shows its link again, so it can be re-sent.
    assert f"https://mcp.garmin.example/signup?code={code}" in page
    assert "Revoke this invitation" in page

    client.post("/admin/invite/delete", data={"code": code})
    assert not users.invite_valid(code)
    assert code not in client.get("/admin").text


def test_deleting_a_used_invitation_keeps_the_account(client, user_id):
    _login(client)
    code = users.create_invite("Bob")
    other = users.create_user("bob@example.com", "anotherlongpw", code)
    client.post("/admin/invite/delete", data={"code": code})
    assert code not in [i["code"] for i in users.list_invites()]
    assert users.get_user(other) is not None       # the account it created stays


def test_a_normal_user_cannot_delete_invitations(client, user_id):
    code = users.create_invite("Anja")
    users.create_user("bob@example.com", "anotherlongpw", users.create_invite())
    _login(client, "bob@example.com", "anotherlongpw")
    assert client.post("/admin/invite/delete", data={"code": code}).status_code == 404
    assert users.invite_valid(code)


def test_cli_invite_delete(capsys, user_id):
    code = users.create_invite("Anja")
    assert main(["invite", "delete", code]) == 0
    assert not users.invite_valid(code)
    assert main(["invite", "delete", "no-such-code"]) == 1


def test_cli_invite_list_prints_the_full_code(capsys, user_id):
    code = users.create_invite("Anja")
    main(["invite", "list"])
    assert code in capsys.readouterr().out       # truncated codes cannot be deleted


def test_paragraphs_are_not_hard_wrapped():
    """Mail clients wrap to the reader's window. Breaking sentences ourselves
    only produces ragged edges - and the breaks move as soon as the host name
    or the link is longer than the placeholder it replaced."""
    from garmin_mcp.web import invitation_mail
    long_host = "https://mcp.garmin.a-rather-long-hostname.example"
    for german in (False, True):
        _, body = invitation_mail(f"{long_host}/signup?code={'x' * 32}",
                                  long_host, german)
        sentences = [line for line in body.split("\n")
                     if line and "://" not in line and not line.startswith("   ")]
        # Every prose paragraph is one line, however long it is.
        assert any(len(line) > 120 for line in sentences)
        for line in body.split("\n"):
            assert not line.endswith(" ")           # no leftover wrap artefacts


def test_structure_still_gets_its_own_lines():
    """Not wrapping prose must not collapse the steps into one block."""
    from garmin_mcp.web import invitation_mail
    _, body = invitation_mail("https://x.example/signup?code=1", "https://x.example")
    lines = body.split("\n")
    assert sum(1 for line in lines if line.startswith(("1. ", "2. ", "3. "))) == 3
    assert sum(1 for line in lines if line.startswith("   - ")) == 3
    assert "       https://x.example/mcp" in lines


def test_urls_survive_wrapping_unbroken():
    from garmin_mcp.web import invitation_mail
    link = "https://mcp.example.com/signup?code=" + "y" * 40
    _, body = invitation_mail(link, "https://mcp.example.com")
    assert link in body                     # not split across two lines
    assert "https://mcp.example.com/mcp" in body


def test_the_german_text_uses_real_umlauts():
    from garmin_mcp.web import invitation_mail
    _, body = invitation_mail("https://x.example/signup?code=1", "https://x.example",
                              german=True)
    assert "persönliche" in body and "löschen" in body
    assert "persoenliche" not in body and "loeschen" not in body
