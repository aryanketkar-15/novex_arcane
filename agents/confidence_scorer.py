def score_resolution(intent_result: dict, ast_diff_result: dict) -> float:
    base_score = float(intent_result.get('confidence', 0.5))
    
    penalty = 0.0
    bonus = 0.0
    
    modified = ast_diff_result.get('modified_functions', [])
    
    if len(modified) == 0:
        penalty += 0.05
    if len(modified) > 3:
        penalty += 0.10
    
    if intent_result.get('compatible') is True:
        bonus += 0.05
    
    final = base_score - penalty + bonus
    # Clamp to [0.0, 1.0]
    return max(0.0, min(1.0, final))
