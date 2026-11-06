import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.dep_graph_builder import build_dep_graph

DEMO_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'arcane-demo-repo'))


def test_dep_graph_finds_importers_of_auth():
    """api.py and middleware.py both import from auth — should appear in auth's imported_by."""
    graph = build_dep_graph(DEMO_REPO)

    # Find the auth entry (may be keyed as 'auth.py')
    auth_key = next((k for k in graph if k.endswith('auth.py')), None)
    assert auth_key is not None, f"auth.py not found in graph. Keys: {list(graph.keys())}"

    imported_by = graph[auth_key]['imported_by']
    importers   = [os.path.basename(p) for p in imported_by]

    assert 'api.py' in importers,        f"api.py missing from imported_by: {importers}"
    assert 'middleware.py' in importers, f"middleware.py missing from imported_by: {importers}"


def test_dep_graph_functions_found():
    """validate_user must appear in auth.py's functions list."""
    graph = build_dep_graph(DEMO_REPO)
    auth_key = next((k for k in graph if k.endswith('auth.py')), None)
    assert auth_key is not None

    assert 'validate_user' in graph[auth_key]['functions'], \
        f"validate_user not found. Functions: {graph[auth_key]['functions']}"


def test_dep_graph_runs_fast():
    """Graph must complete in under 5 seconds for demo repo."""
    import time
    start = time.time()
    build_dep_graph(DEMO_REPO)
    elapsed = time.time() - start
    assert elapsed < 5.0, f"Took {elapsed:.2f}s — too slow!"
