import os
import re

def detect_conflicts(filepath: str) -> list[dict]:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            content = "".join(lines)
    except OSError:
        return []

    # Regex to find conflict blocks
    # Note: re.DOTALL is important for multi-line
    pattern = re.compile(
        r'<<<<<<<.*?\n(.*?)=======\n(.*?)>>>>>>>.*?\n', 
        re.DOTALL
    )
    
    conflicts = []
    
    # We find all matches, but we also want start/end lines.
    # We can do this by iterating line by line or parsing the big string.
    # Iterating lines is safer for exact line numbers:
    
    in_conflict = False
    current_ours = []
    current_theirs = []
    start_line = -1
    stage = 0 # 0=normal, 1=ours, 2=theirs
    
    for i, line in enumerate(lines, start=1):
        if line.startswith('<<<<<<<'):
            in_conflict = True
            start_line = i
            stage = 1
            current_ours = []
            current_theirs = []
        elif line.startswith('======='):
            stage = 2
        elif line.startswith('>>>>>>>'):
            in_conflict = False
            end_line = i
            stage = 0
            
            # Reconstruct the code keeping lines valid
            ours_code = "".join(current_ours)
            theirs_code = "".join(current_theirs)
            
            conflicts.append({
                'ours': ours_code,
                'theirs': theirs_code,
                'start_line': start_line,
                'end_line': end_line,
                'file': filepath
            })
        else:
            if stage == 1:
                current_ours.append(line)
            elif stage == 2:
                current_theirs.append(line)
                
    return conflicts
