import os
import pytest
import sys
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.intent_inferrer import infer_intent, _parse_response
from agents.confidence_scorer import score_resolution
from agents.auto_resolver import resolve


# ─── Intent Inferrer Tests ────────────────────────────────────────────────────

def test_infer_intent_mock_llm():
    mock_response = json.dumps({
        "winner": "ours",
        "reasoning": "Ours correctly checks both username and password.",
        "compatible": False,
        "confidence": 0.92
    })
    
    with patch('agents.intent_inferrer._call_claude', return_value=mock_response), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key"}):
        
        result = infer_intent(
            ours_code="if username == 'admin' and password == 'secret':\n    return True",
            theirs_code="if not username or not password:\n    return False",
            ours_commit_msg="fix(auth): validate both username and password",
            theirs_commit_msg="fix(auth): add null check"
        )
    
    assert result['winner'] == 'ours'
    assert isinstance(result['confidence'], float)
    assert 0.0 <= result['confidence'] <= 1.0
    assert isinstance(result['compatible'], bool)
    assert 'reasoning' in result


def test_parse_response_strips_markdown():
    raw = "```json\n{\"winner\": \"theirs\", \"reasoning\": \"test\", \"compatible\": true, \"confidence\": \"0.87\"}\n```"
    result = _parse_response(raw)
    
    assert result['winner'] == 'theirs'
    assert isinstance(result['confidence'], float)
    assert result['confidence'] == 0.87


# ─── Confidence Scorer Tests ─────────────────────────────────────────────────

def test_score_resolution_high_confidence():
    intent = {'confidence': 0.90, 'compatible': True}
    diff = {'modified_functions': ['validate_user']}
    score = score_resolution(intent, diff)
    assert isinstance(score, float)
    assert score == pytest.approx(0.95)   # 0.90 - 0 penalty + 0.05 bonus

def test_score_resolution_clamped():
    intent = {'confidence': 1.0, 'compatible': True}
    diff = {'modified_functions': ['a', 'b', 'c', 'd', 'e']}  # >3, penalty 0.10
    score = score_resolution(intent, diff)
    assert score == pytest.approx(0.95)   # 1.0 - 0.10 + 0.05


# ─── Auto Resolver Tests ──────────────────────────────────────────────────────

def test_auto_resolve_high_score(tmp_path):
    conflicted_file = tmp_path / "conflicted.py"
    conflicted_file.write_text(
        "<<<<<<< HEAD\n"
        "def validate_user(username, password):\n"
        "    return True\n"
        "=======\n"
        "def validate_user(user, pwd):\n"
        "    return False\n"
        ">>>>>>> feature\n"
    )
    
    conflict = {
        'ours': "def validate_user(username, password):\n    return True",
        'theirs': "def validate_user(user, pwd):\n    return False",
        'start_line': 1,
        'end_line': 7,
        'file': str(conflicted_file)
    }
    intent = {'winner': 'ours', 'reasoning': 'Ours is better', 'compatible': False, 'confidence': 0.92}
    
    result = resolve(conflict, intent, score=0.92)
    assert result['action'] == 'auto_resolved'
    assert result['score'] == 0.92


def test_needs_review_middle_score():
    conflict = {'ours': 'x', 'theirs': 'y', 'start_line': 1, 'end_line': 5, 'file': ''}
    intent = {'winner': 'ours', 'reasoning': 'Unclear', 'compatible': False, 'confidence': 0.70}
    result = resolve(conflict, intent, score=0.70)
    assert result['action'] == 'needs_review'


def test_escalate_low_score():
    conflict = {'ours': 'x', 'theirs': 'y', 'start_line': 1, 'end_line': 5, 'file': ''}
    intent = {'winner': 'theirs', 'reasoning': 'Cannot decide', 'compatible': False, 'confidence': 0.40}
    result = resolve(conflict, intent, score=0.40, github_repo=None)
    assert result['action'] == 'escalated'
