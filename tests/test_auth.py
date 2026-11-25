from types import SimpleNamespace

import pytest

from mirage.auth import AuthContext, AuthService, SESSION_COOKIE
from mirage.errors import WorkbenchError


class Request:
    def __init__(self, path="/api/v1/jobs", method="GET", headers=None, cookies=None):
        self.url = SimpleNamespace(path=path)
        self.method = method
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_browser_session_and_bearer_roles(monkeypatch):
    monkeypatch.setenv("MIRAGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("MIRAGE_BROWSER_TOKEN", "browser-secret")
    service = AuthService()
    session_id, context = service.login("browser-secret")
    assert context.role == "user"
    request = Request(cookies={SESSION_COOKIE: session_id})
    assert service.authenticate(request).csrf == context.csrf
    assert service.requires_csrf(Request(method="POST"), context)
    assert service.requires_admin(Request(path="/api/v1/settings", method="PUT"))
    assert not service.requires_admin(Request(path="/api/v1/jobs", method="POST"))
    bearer = service.authenticate(Request(headers={"Authorization": "Bearer admin-secret"}))
    assert bearer.role == "admin"
    assert not service.requires_csrf(Request(method="POST"), bearer)
    service.logout(request)
    assert service.authenticate(request) is None


def test_invalid_login_and_expired_session(monkeypatch):
    monkeypatch.setenv("MIRAGE_API_TOKEN", "admin-secret")
    service = AuthService()
    with pytest.raises(WorkbenchError):
        service.login("wrong")
    session_id, _ = service.login("admin-secret")
    service.sessions[service._digest(session_id)]["expires"] = 0
    assert service.authenticate(Request(cookies={SESSION_COOKIE: session_id})) is None
