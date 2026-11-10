"""Private model credentials. Only authenticated owners can manage their keys."""
from contextlib import closing, contextmanager
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr

from api.auth import decrypt_token, encrypt_token, require_user
from api.db import _get_postgres_connection, _get_sqlite_connection, is_postgres_configured

router = APIRouter(prefix="/api/settings/model-keys")


def owner(user=Depends(require_user)):
    if not user or not user.get("login"):
        raise HTTPException(401, "Sign in with GitHub to manage personal API keys.")
    return user["login"]


@contextmanager
def database():
    pg = is_postgres_configured()
    conn = _get_postgres_connection() if pg else _get_sqlite_connection()
    if conn is None:
        raise RuntimeError("Model credential database unavailable")
    with closing(conn), conn, closing(conn.cursor()) as cur:
        identity = "BIGSERIAL" if pg else "INTEGER"
        cur.execute(f"""CREATE TABLE IF NOT EXISTS model_keys (
            id {identity} PRIMARY KEY, owner TEXT NOT NULL, provider TEXT NOT NULL,
            model TEXT NOT NULL, ciphertext TEXT NOT NULL)""")
        yield cur, "%s" if pg else "?"


class ModelKey(BaseModel):
    provider: Literal["google", "openai", "groq"]
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    key: SecretStr


@router.get("")
def list_keys(response: Response, login=Depends(owner)):
    response.headers["Cache-Control"] = "no-store"
    with database() as (cur, p):
        cur.execute(f"SELECT id, provider, model FROM model_keys WHERE owner={p} ORDER BY id", (login,))
        return [dict(zip(("id", "provider", "model"), row)) for row in cur.fetchall()]


@router.post("", status_code=201)
def add_key(body: ModelKey, login=Depends(owner)):
    key = body.key.get_secret_value().strip()
    if not key or len(key) > 4096:
        raise HTTPException(400, "Enter a valid API key.")
    with database() as (cur, p):
        cur.execute(f"INSERT INTO model_keys (owner,provider,model,ciphertext) VALUES ({','.join([p]*4)})",
                    (login, body.provider, body.model, encrypt_token(key)))
    return {"ok": True}


@router.delete("/{key_id}")
def remove_key(key_id: int, login=Depends(owner)):
    with database() as (cur, p):
        cur.execute(f"DELETE FROM model_keys WHERE owner={p} AND id={p}", (login, key_id))
    return {"ok": True}


def run_credentials(login):
    if not login:
        return []
    with database() as (cur, p):
        cur.execute(f"SELECT provider, model, ciphertext FROM model_keys WHERE owner={p} ORDER BY id", (login,))
        rows = cur.fetchall()
    return [(provider, model, decrypt_token(cipher)) for provider, model, cipher in rows]
