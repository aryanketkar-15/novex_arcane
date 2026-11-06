import os

def resolve(conflict: dict, intent_result: dict, score: float,
            github_repo=None, commit_sha: str = "") -> dict:
    
    winner = intent_result.get('winner', 'ours')
    
    if score > 0.85:
        # Auto-resolve: write winning code to file in-place
        winning_code = conflict['ours'] if winner != 'theirs' else conflict['theirs']
        
        filepath = conflict.get('file', '')
        if filepath and os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Rebuild file — replace the whole conflict block with winning code
            lines = content.splitlines(keepends=True)
            start = conflict['start_line'] - 1
            end = conflict['end_line']
            
            new_lines = lines[:start] + [winning_code + "\n"] + lines[end:]
            with open(filepath, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
        
        return {'action': 'auto_resolved', 'score': score, 'winner': winner}
    
    elif score < 0.60:
        # Escalate — post GitHub issue comment
        if github_repo:
            try:
                body = _build_escalation_body(conflict, intent_result, score, commit_sha)
                github_repo.create_issue(
                    title=f"[ARCANE] Conflict escalation in {conflict.get('file', 'unknown')}",
                    body=body
                )
            except Exception as e:
                print(f"GitHub escalation failed: {e}")
        
        return {'action': 'escalated', 'score': score}
    
    else:
        # Needs human review
        return {'action': 'needs_review', 'score': score}


def _build_escalation_body(conflict: dict, intent_result: dict, score: float, commit_sha: str) -> str:
    return f"""## ⚠️ ARCANE Conflict Escalation

**File:** `{conflict.get('file', 'unknown')}`
**Commit:** `{commit_sha}`
**Confidence Score:** `{score:.2f}` (below auto-resolve threshold of 0.60)

---

### Our Version
```python
{conflict.get('ours', '')}
```

### Their Version
```python
{conflict.get('theirs', '')}
```

---

### LLM Reasoning
> {intent_result.get('reasoning', 'No reasoning provided.')}

**Compatible:** {intent_result.get('compatible', False)}
**Suggested Winner:** {intent_result.get('winner', 'unknown')}

---
*Please resolve this conflict manually and close this issue.*
"""
