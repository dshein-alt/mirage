import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass

from .errors import WorkbenchError

SESSION_COOKIE = "mirage_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class AuthContext:
    role: str
    method: str
    csrf: str = ""


class AuthService:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.RLock()

    @staticmethod
    def enabled(host):
        return host not in LOCAL_HOSTS

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _matches(provided, expected):
        return bool(provided and expected and secrets.compare_digest(provided, expected))

    def authenticate(self, request):
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            if self._matches(authorization.removeprefix("Bearer "), os.environ.get("MIRAGE_API_TOKEN")):
                return AuthContext("admin", "bearer")
            return None
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        key = self._digest(session_id)
        with self.lock:
            session = self.sessions.get(key)
            if not session:
                return None
            if session["expires"] <= time.time():
                self.sessions.pop(key, None)
                return None
            return AuthContext(session["role"], "cookie", session["csrf"])

    def login(self, token):
        if self._matches(token, os.environ.get("MIRAGE_API_TOKEN")):
            role = "admin"
        elif self._matches(token, os.environ.get("MIRAGE_BROWSER_TOKEN")):
            role = "user"
        else:
            raise WorkbenchError("unauthorized", "Invalid access token", 401)
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[self._digest(session_id)] = {
                "role": role,
                "csrf": csrf,
                "expires": time.time() + SESSION_SECONDS,
            }
        return session_id, AuthContext(role, "cookie", csrf)

    def logout(self, request):
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            with self.lock:
                self.sessions.pop(self._digest(session_id), None)

    @staticmethod
    def requires_csrf(request, context):
        return context.method == "cookie" and request.method in MUTATIONS

    @staticmethod
    def requires_admin(request):
        path = request.url.path
        if request.method not in MUTATIONS:
            return False
        if "/runtime" in path or "/gallery/reindex" in path:
            return True
        if path.endswith("/remove") and "/models/" in path:
            return True
        return "/config/generation" in path or path.endswith("/settings")
