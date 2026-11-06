import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.dep_graph_builder import build_dep_graph
from agents.caller_finder import find_callers
from agents.cross_file_propagator import CrossFilePropagator

DEMO_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'arcane-demo-repo'))


def test_find_callers_for_validate_user():
    """validate_user lives in auth.py — api.py and middleware.py should be returned."""
    graph = build_dep_graph(DEMO_REPO)
    callers = find_callers('validate_user', graph)
    basenames = [os.path.basename(c) for c in callers]

    assert 'api.py' in basenames,        f"api.py missing: {basenames}"
    assert 'middleware.py' in basenames, f"middleware.py missing: {basenames}"


def test_find_callers_unknown_function():
    """Unknown function should return empty list, not crash."""
    graph = build_dep_graph(DEMO_REPO)
    callers = find_callers('nonexistent_function_xyz', graph)
    assert callers == []


def test_propagate_returns_patch_dicts():
    """propagate() should return 2 patch dicts for the demo repo scenario."""
    # Mock call_llm so we don't need a real LLM during tests
    mock_updated = (
        "from auth import validate_user\n\n"
        "def some_func():\n"
        "    return validate_user('admin', 'secret')\n"
    )

    with patch('agents.multi_file_patcher.call_llm', return_value=mock_updated):
        propagator = CrossFilePropagator()
        state = {
            'repo_path':          DEMO_REPO,
            'changed_function':   'validate_user',
            'original_signature': 'validate_user(username, password)',
            'new_signature':      'validate_user(user, pwd)'
        }
        result = propagator.propagate(state)

    assert result['propagation_done'] is True
    assert len(result['propagated_files']) >= 2
    assert len(result['caller_patches']) >= 2

    for patch_item in result['caller_patches']:
        assert 'file' in patch_item
        assert 'patch' in patch_item
