from __future__ import annotations

import logging
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import AuthConfig, resolve_auth_password

logger = logging.getLogger("watchtower.auth")

_security = HTTPBasic(auto_error=False)


def make_auth_dependency(auth_cfg: AuthConfig):
    """Returns a FastAPI dependency. If auth is disabled, the dependency is
    a no-op -- callers still get a consistent Depends(...) wiring point
    either way, so routes don't need an if/else at the call site."""
    if not auth_cfg.enabled:
        async def noop_auth():
            return None
        return noop_auth

    username = auth_cfg.username
    password = resolve_auth_password(auth_cfg)
    # load_config() already guarantees `password` is set when enabled=True,
    # but this module can in principle be used standalone -- fail loudly
    # rather than silently accepting every request if that invariant is
    # ever violated by a caller that skipped validation.
    if not password:
        raise ValueError("make_auth_dependency called with auth.enabled=True but no resolvable password")

    async def check_auth(credentials: HTTPBasicCredentials = Depends(_security)):
        if credentials is None:
            raise HTTPException(
                status_code=401, detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        # constant-time comparison -- avoids leaking match-length via timing
        user_ok = secrets.compare_digest(credentials.username, username)
        pass_ok = secrets.compare_digest(credentials.password, password)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401, detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    return check_auth
