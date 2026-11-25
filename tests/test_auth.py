from types import SimpleNamespace

import pytest

from mirage.auth import AuthContext, AuthService, SESSION_COOKIE
from mirage.errors import WorkbenchError

ADMIN_TOKEN = "admin-secret_12345"
BROWSER_TOKEN = "browser-secret_12345"


class Request:
    def __init__(self, path="/api/v1/jobs", method="GET", headers=None, cookies=None):
        self.url = SimpleNamespace(path=path)
        self.method = method
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_browser_session_and_bearer_roles(monkeypatch):
    monkeypatch.setenv("MIRAGE_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("MIRAGE_BROWSER_TOKEN", BROWSER_TOKEN)
    service = AuthService("authenticated")
    assert service.enabled()
    session_id, context = service.login(BROWSER_TOKEN)
    assert context.role == "user"
    request = Request(cookies={SESSION_COOKIE: session_id})
    assert service.authenticate(request).csrf == context.csrf
    assert service.requires_csrf(Request(method="POST"), context)
    assert service.requires_admin(Request(path="/api/v1/settings", method="PUT"))
    assert not service.requires_admin(Request(path="/api/v1/jobs", method="POST"))
    bearer = service.authenticate(Request(headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}))
    assert bearer.role == "admin"
    assert not service.requires_csrf(Request(method="POST"), bearer)
    service.logout(request)
    assert service.authenticate(request) is None


def test_invalid_login_and_expired_session(monkeypatch):
    monkeypatch.setenv("MIRAGE_API_TOKEN", ADMIN_TOKEN)
    service = AuthService()
    assert not service.enabled()
    with pytest.raises(WorkbenchError):
        service.login("wrong")
    session_id, _ = service.login(ADMIN_TOKEN)
    service.sessions[service._digest(session_id)]["expires"] = 0
    assert service.authenticate(Request(cookies={SESSION_COOKIE: session_id})) is None


def test_local_mode_does_not_require_tokens(monkeypatch):
    monkeypatch.delenv("MIRAGE_API_TOKEN", raising=False)
    monkeypatch.delenv("MIRAGE_BROWSER_TOKEN", raising=False)
    assert not AuthService("local").enabled()


@pytest.mark.parametrize("token", ["", "short", "a" * 65, "contains spaces 1234", "unsafe.token.1234"])
def test_authenticated_mode_rejects_invalid_tokens(monkeypatch, token):
    monkeypatch.setenv("MIRAGE_API_TOKEN", token)
    monkeypatch.setenv("MIRAGE_BROWSER_TOKEN", BROWSER_TOKEN)
    with pytest.raises(ValueError, match="MIRAGE_API_TOKEN"):
        AuthService("authenticated")


def test_authenticated_mode_requires_browser_token(monkeypatch):
    monkeypatch.setenv("MIRAGE_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.delenv("MIRAGE_BROWSER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="MIRAGE_BROWSER_TOKEN"):
        AuthService("authenticated")


def test_authenticated_mode_requires_distinct_tokens(monkeypatch):
    monkeypatch.setenv("MIRAGE_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("MIRAGE_BROWSER_TOKEN", ADMIN_TOKEN)
    with pytest.raises(ValueError, match="different"):
        AuthService("authenticated")
