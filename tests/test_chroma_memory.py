import os
import sys
import shutil
import tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.chroma_memory import init_memory, store_patch, query_memory


@pytest.fixture
def chroma_client(tmp_path):
    """Fresh ChromaDB client for every test."""
    client = init_memory(str(tmp_path / 'chroma_test'))
    yield client


def test_store_then_query_matches(chroma_client):
    """Storing a patch and querying with the same error_log should return a match."""
    error_log  = "ImportError: cannot import name 'validate_user' from 'auth'"
    root_cause = "Function signature changed from validate_user(username, password) to validate_user(user, pwd)"
    patch_diff = "--- a/api.py\n+++ b/api.py\n@@ -1 +1 @@\n-from auth import validate_user\n+from auth import validate_user as vu"
    test_file  = "tests/test_api.py"
    commit_sha = "abc123def456"

    store_patch(chroma_client, error_log, root_cause, patch_diff, test_file, commit_sha)

    # Query with same error — must be a match
    result = query_memory(chroma_client, error_log, threshold=0.85)

    assert result is not None, "Expected a memory match but got None"
    assert result['commit_sha'] == commit_sha
    assert result['test_file']  == test_file
    assert result['patch_diff'] == patch_diff
    assert 'date' in result


def test_query_unrelated_returns_none(chroma_client):
    """Querying with a completely unrelated error should return None."""
    # Store one patch
    store_patch(
        chroma_client,
        error_log  = "ImportError: cannot import name 'validate_user' from 'auth'",
        root_cause = "Signature mismatch",
        patch_diff = "--- a/api.py\n+++ b/api.py",
        test_file  = "tests/test_api.py",
        commit_sha = "abc123"
    )

    # Query with something completely unrelated
    result = query_memory(
        chroma_client,
        "RuntimeError: CUDA out of memory on GPU device 0",
        threshold=0.85
    )

    assert result is None, f"Expected None for unrelated query but got: {result}"


def test_query_empty_collection_returns_none(chroma_client):
    """Querying an empty collection must not crash — returns None."""
    result = query_memory(chroma_client, "some error", threshold=0.85)
    assert result is None
