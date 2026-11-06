import os
import sys
import json
import tempfile
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.conflict_resolver import ConflictResolver


MOCK_INTENT = {
    'winner': 'ours',
    'reasoning': 'Ours correctly validates both fields.',
    'compatible': False,
    'confidence': 0.92
}


def test_check_no_conflict(tmp_path):
    """Clean file returns no_conflict immediately."""
    clean = tmp_path / "clean.py"
    clean.write_text("def validate_user(username, password):\n    return True\n")

    state = {
        'failing_file':      str(clean),
        'ours_commit_msg':   'fix(auth): check password',
        'theirs_commit_msg': 'fix(auth): null check',
        'commit_sha':        'abc123'
    }

    result = ConflictResolver().check(state)

    assert result['conflicts_found'] is False
    assert result['action'] == 'no_conflict'
    assert result['score'] == 1.0
    assert result['details'] == []


def test_check_auto_resolved(tmp_path):
    """High-confidence conflict gets auto-resolved."""
    conflicted = tmp_path / "conflicted.py"
    conflicted.write_text(
        "<<<<<<< HEAD\n"
        "def validate_user(username, password):\n"
        "    return True\n"
        "=======\n"
        "def validate_user(user, pwd):\n"
        "    return False\n"
        ">>>>>>> feature\n"
    )

    state = {
        'failing_file':      str(conflicted),
        'ours_commit_msg':   'fix(auth): validate both fields',
        'theirs_commit_msg': 'refactor(auth): rename params',
        'commit_sha':        'def456'
    }

    mock_resp = json.dumps(MOCK_INTENT)

    with patch('agents.intent_inferrer._call_claude', return_value=mock_resp), \
         patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fake-key'}):
        result = ConflictResolver().check(state)

    assert result['conflicts_found'] is True
    assert result['action'] == 'auto_resolved'
    assert result['score'] > 0.85
    assert len(result['details']) == 1
