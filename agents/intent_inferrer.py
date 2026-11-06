import os
import json
import httpx

def infer_intent(ours_code: str, theirs_code: str,
                 ours_commit_msg: str, theirs_commit_msg: str) -> dict:
    
    prompt = f"""You are a senior software engineer reviewing a Git merge conflict.

OURS (commit: "{ours_commit_msg}"):
```python
{ours_code}
```

THEIRS (commit: "{theirs_commit_msg}"):
```python
{theirs_code}
```

Answer the following:
1. Which change better represents the intended system behavior?
2. Are these changes compatible or mutually exclusive?

Return ONLY a JSON object with no preamble, no markdown fences, exactly this shape:
{{"winner": "ours" | "theirs" | "merge", "reasoning": "one sentence explanation", "compatible": true | false, "confidence": 0.0}}"""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    
    if api_key:
        try:
            result = _call_claude(prompt, api_key)
            return _parse_response(result)
        except Exception as e:
            print(f"Claude API failed: {e}. Falling back to Ollama.")
    
    # Ollama fallback
    ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        result = _call_ollama(prompt, ollama_base)
        return _parse_response(result)
    except Exception as e:
        print(f"Ollama fallback also failed: {e}. Returning safe default.")
        return {
            "winner": "ours",
            "reasoning": "Could not infer intent — defaulting to ours.",
            "compatible": False,
            "confidence": 0.5
        }


def _call_claude(prompt: str, api_key: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def _call_ollama(prompt: str, base_url: str) -> str:
    response = httpx.post(
        f"{base_url}/api/generate",
        json={"model": "llama3", "prompt": prompt, "stream": False},
        timeout=60.0
    )
    response.raise_for_status()
    return response.json()["response"]


def _parse_response(text: str) -> dict:
    # Strip markdown fences if present
    cleaned = text.replace("```json", "").replace("```", "").strip()
    
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        # Retry: extract first {...} block
        import re
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            raise ValueError(f"Could not parse LLM response as JSON: {cleaned}")
    
    # Defensive type coercions
    result['confidence'] = float(result.get('confidence', 0.5))
    result['compatible'] = bool(result.get('compatible', False))
    result.setdefault('winner', 'ours')
    result.setdefault('reasoning', '')
    
    return result
