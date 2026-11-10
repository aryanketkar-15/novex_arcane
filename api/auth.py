"""GitHub OAuth login, per-user sessions and repository access.

Security Architecture:
- Uses random opaque session IDs (`secrets.token_urlsafe(32)`) in browser cookies.
- No sensitive credentials, tokens, or usernames are placed in cookie payloads.
- Cookies are strictly configured with `HttpOnly`, `SameSite=Lax`, and `Secure` (in HTTPS/hosted environments).
- OAuth access tokens are encrypted at rest using AES/Fernet with a securely derived server-side key.
- Requires configured `SECRET_KEY` or `GITHUB_CLIENT_SECRET` in hosted production mode.
- Enforces session expiration (14 days) and immediate server-side logout invalidation.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from api.store import DATA_DIR, default_repo, now_iso, store
from api.db import db_delete_session, db_get_session, db_load_all_sessions, db_save_session

logger = logging.getLogger(__name__)
auth_router = APIRouter(prefix="/api")

SESSION_FILE = os.path.join(DATA_DIR, "sessions.json")
COOKIE = "arcane_session"
SCOPES = "repo admin:repo_hook read:user user:email"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 3600  # 14 days

_lock = threading.Lock()
_states: set[str] = set()


def _get_encryption_key() -> bytes:
    """Derives a 32-byte urlsafe base64 key for Fernet encryption.
    Strictly forbids running without configured secrets in hosted mode."""
    secret = os.getenv("SECRET_KEY") or os.getenv("GITHUB_CLIENT_SECRET")
    if not secret:
        from agents.execution_mode import is_hosted_mode
        if is_hosted_mode():
            raise RuntimeError(
                "CRITICAL SECURITY CONFIGURATION ERROR: SECRET_KEY or GITHUB_CLIENT_SECRET "
                "must be configured in hosted mode to encrypt session credentials at rest. "
                "Refusing to run with default or empty secrets in production."
            )
        # In local development only, allow GITHUB_WEBHOOK_SECRET
        secret = os.getenv("GITHUB_WEBHOOK_SECRET")
        if not secret:
            raise RuntimeError(
                "Security configuration error: At least one of SECRET_KEY, "
                "GITHUB_CLIENT_SECRET, or GITHUB_WEBHOOK_SECRET must be set."
            )

    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def encrypt_token(token: str) -> str:
    """Encrypts a sensitive credential at rest."""
    if not token:
        return ""
    from cryptography.fernet import Fernet
    f = Fernet(_get_encryption_key())
    return f.encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(enc_token: str) -> str:
    """Decrypts a sensitive credential in memory."""
    if not enc_token:
        return ""
    from cryptography.fernet import Fernet
    try:
        f = Fernet(_get_encryption_key())
        return f.decrypt(enc_token.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"[Auth] Decryption of session token failed: {e}")
        return ""


def oauth_configured() -> bool:
    return bool(os.getenv("GITHUB_CLIENT_ID") and os.getenv("GITHUB_CLIENT_SECRET"))


def app_url() -> str:
    return os.getenv("APP_URL", "http://localhost:5173").rstrip("/")


def is_secure_cookie() -> bool:
    """Ensures Secure cookie flag is enabled in hosted mode or on HTTPS."""
    from agents.execution_mode import is_hosted_mode
    return is_hosted_mode() or app_url().startswith("https://")


def _load() -> dict:
    # 1. Try loading from SQLite database
    try:
        db_sessions = db_load_all_sessions()
        if db_sessions:
            return db_sessions
    except Exception:
        pass

    # 2. Fall back to JSON file
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(sessions: dict) -> None:
    # 1. Save to JSON file
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f)
    os.replace(tmp, SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except Exception:
        pass

    # 2. Also mirror to SQLite database
    for sid, u in sessions.items():
        try:
            db_save_session(sid, u)
        except Exception:
            pass


def current_user(request: Request) -> Optional[dict]:
    """Returns the signed-in user with active in-memory token, or None.
    Uses opaque session IDs only. Validates session expiry and decrypts credentials."""
    sid = request.cookies.get(COOKIE)
    if not sid:
        return None

    # Handle opaque session ID
    with _lock:
        sessions = _load()
        user = sessions.get(sid) or db_get_session(sid)
        if not user:
            return None

        # 1. Validate expiration
        expires_at = user.get("expiresAt")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at)
                if datetime.now(timezone.utc) > exp_dt:
                    logger.info(f"[Auth] Session {sid[:8]}... expired. Invaliding.")
                    sessions.pop(sid, None)
                    _save(sessions)
                    try:
                        db_delete_session(sid)
                    except Exception:
                        pass
                    return None
            except Exception as e:
                logger.warning(f"[Auth] Could not parse expiration date: {e}")

        # 2. Decrypt credential in-memory
        token = decrypt_token(user.get("encrypted_token", ""))
        if not token and user.get("token"):
            # Backward compatibility for legacy sessions
            token = user.get("token")

        return {
            **user,
            "token": token,
        }


def require_user(request: Request) -> Optional[dict]:
    """Dependency: 401 when OAuth is enabled and nobody is signed in."""
    user = current_user(request)
    if user is None and oauth_configured():
        raise HTTPException(401, "Sign in with GitHub to continue.")
    return user


def public_user(user: Optional[dict]) -> Optional[dict]:
    if not user:
        return None
    return {k: user.get(k) for k in ("login", "name", "email", "avatar")}


def token_for_login(login: str) -> Optional[str]:
    with _lock:
        for u in _load().values():
            if u.get("login") == login:
                enc = u.get("encrypted_token")
                if enc:
                    return decrypt_token(enc)
                return u.get("token")
    return None


def token_for_repo(repo: str) -> Optional[str]:
    """Token of the user who watches `repo` (used for webhook-triggered runs)."""
    login = store.watched.get(repo)
    if login:
        tok = token_for_login(login)
        if tok:
            return tok
    owner = repo.split("/")[0] if "/" in repo else repo
    tok = token_for_login(owner)
    if tok:
        return tok
    with _lock:
        sessions = _load()
        for u in sessions.values():
            enc = u.get("encrypted_token")
            tok = decrypt_token(enc) if enc else u.get("token")
            if tok:
                return tok
    return None


# ── OAuth flow ───────────────────────────────────────────────────────────────

@auth_router.get("/auth/config")
def auth_config():
    return {"oauth": oauth_configured()}


@auth_router.get("/auth/github/login")
def login():
    if not oauth_configured():
        raise HTTPException(503, "GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET are not set in .env")
    state = secrets.token_urlsafe(24)
    _states.add(state)
    q = urlencode(
        {
            "client_id": os.environ["GITHUB_CLIENT_ID"],
            "redirect_uri": f"{app_url()}/api/auth/github/callback",
            "scope": SCOPES,
            "state": state,
        }
    )
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{q}")


@auth_router.get("/auth/github/callback")
def callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"{app_url()}/?auth_error={error}")
    if state not in _states:
        return RedirectResponse(f"{app_url()}/?auth_error=invalid_state")
    _states.discard(state)
    try:
        tok = httpx.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": os.environ["GITHUB_CLIENT_ID"],
                "client_secret": os.environ["GITHUB_CLIENT_SECRET"],
                "code": code,
                "redirect_uri": f"{app_url()}/api/auth/github/callback",
            },
            headers={"Accept": "application/json"},
            timeout=15,
        ).json()
        token = tok.get("access_token")
        if not token:
            logger.warning(f"[Auth] token exchange failed: {tok.get('error')}")
            return RedirectResponse(f"{app_url()}/?auth_error={tok.get('error', 'token_exchange_failed')}")
        gh = httpx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        ).json()
    except Exception as e:
        logger.error(f"[Auth] OAuth error: {e}")
        return RedirectResponse(f"{app_url()}/?auth_error=network")

    # Generate random opaque session ID — no credentials in cookie
    sid = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat()

    user_record = {
        "login": gh.get("login"),
        "name": gh.get("name") or gh.get("login"),
        "email": gh.get("email") or "",
        "avatar": gh.get("avatar_url") or "",
        "encrypted_token": encrypt_token(token),
        "createdAt": now_iso(),
        "expiresAt": expires_at,
    }
    with _lock:
        sessions = _load()
        sessions[sid] = user_record
        _save(sessions)
    store.add_audit("Signed in with GitHub", "Workspace", gh.get("login", "unknown"))

    resp = RedirectResponse(f"{app_url()}/")
    # Set HttpOnly, Secure, SameSite=Lax cookie with OPAQUE session ID only
    resp.set_cookie(
        COOKIE,
        sid,
        httponly=True,
        secure=is_secure_cookie(),
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
    )
    return resp


@auth_router.get("/me")
def me(request: Request):
    user = current_user(request)
    if user is None and oauth_configured():
        raise HTTPException(401, "Not signed in")
    pub = public_user(user) or {"login": "local", "name": "Local user", "email": "", "avatar": ""}
    pub["workspace"] = store.settings["workspace"]
    pub["oauth"] = oauth_configured()
    return pub


@auth_router.post("/auth/logout")
def logout(request: Request, response: Response):
    sid = request.cookies.get(COOKIE)
    if sid:
        with _lock:
            s = _load()
            s.pop(sid, None)
            _save(s)
            try:
                db_delete_session(sid)
            except Exception:
                pass
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ── repositories ─────────────────────────────────────────────────────────────

@auth_router.get("/github/repos")
def my_repos(user=Depends(require_user)):
    token = (user or {}).get("token") or os.getenv("GITHUB_PAT")
    if not token:
        raise HTTPException(400, "No GitHub token available.")
    try:
        r = httpx.get(
            "https://api.github.com/user/repos",
            params={"per_page": 100, "sort": "pushed", "affiliation": "owner,collaborator,organization_member"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"[Auth] repos error: {r.status_code} {r.text[:200]}")
            return []
        repos = []
        for repo in r.json():
            repos.append(
                {
                    "fullName": repo.get("full_name"),
                    "name": repo.get("name"),
                    "owner": repo.get("owner", {}).get("login"),
                    "private": repo.get("private", False),
                    "defaultBranch": repo.get("default_branch", "main"),
                    "updatedAt": repo.get("pushed_at") or repo.get("updated_at"),
                }
            )
        return repos
    except Exception as e:
        logger.error(f"[Auth] fetch repos error: {e}")
        return []
