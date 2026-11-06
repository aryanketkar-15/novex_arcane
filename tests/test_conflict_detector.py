import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.conflict_detector import detect_conflicts

def test_detects_conflict_block(tmp_path):
    conflict_file = tmp_path / "conflicted.py"
    conflict_file.write_text(
        "def validate_user(username, password):\n"
        "<<<<<<< HEAD\n"
        "    if username == 'admin' and password == 'secret':\n"
        "        return True\n"
        "=======\n"
        "    if not username or not password:\n"
        "        return False\n"
        ">>>>>>> feature-branch\n"
        "    return False\n"
    )
    
    results = detect_conflicts(str(conflict_file))
    
    assert len(results) == 1
    assert "admin" in results[0]['ours']
    assert "not username" in results[0]['theirs']
    assert results[0]['start_line'] == 2
    assert results[0]['file'] == str(conflict_file)

def test_no_conflict_returns_empty(tmp_path):
    clean_file = tmp_path / "clean.py"
    clean_file.write_text(
        "def validate_user(username, password):\n"
        "    if username == 'admin' and password == 'secret':\n"
        "        return True\n"
        "    return False\n"
    )
    
    results = detect_conflicts(str(clean_file))
    assert results == []
