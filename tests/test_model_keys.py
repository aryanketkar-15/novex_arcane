from fastapi import Response
import requests

from agents import llm_client as llm
from api import db, model_keys


def test_key_rotation_and_provider_fallback(monkeypatch):
    attempts = []
    def generate(provider, model, key, prompt, system):
        attempts.append((provider, key))
        if provider == "google":
            raise requests.Timeout()
        return "repair"
    monkeypatch.setattr(llm, "generate", generate)
    monkeypatch.setattr(llm, "server_credentials", lambda: [("groq", "chat", "third")])
    context = llm.personal_credentials.set([("google", "flash", "first"), ("google", "flash", "second")])
    try:
        assert llm.call_llm("test") == "repair"
        assert attempts == [("google", "first"), ("google", "second"), ("groq", "third")]
    finally:
        llm.personal_credentials.reset(context)


def test_private_key_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "test.db"))
    model_keys.add_key(model_keys.ModelKey(provider="openai", model="chat", key="private-test-key"), "alice")
    row, = model_keys.list_keys(Response(), "alice")
    assert set(row) == {"id", "model", "provider"}
    assert b"private-test-key" not in (tmp_path / "test.db").read_bytes()
    assert model_keys.run_credentials("bob") == []
    model_keys.remove_key(row["id"], "bob")
    assert model_keys.run_credentials("alice") == [("openai", "chat", "private-test-key")]
    model_keys.remove_key(row["id"], "alice")
    assert model_keys.run_credentials("alice") == []
