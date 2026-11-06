import os
import logging
from google import genai
from google.genai import types
import httpx
import settings

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def call_llm(prompt: str, system: str = '') -> str:
    """
    Calls Google Gemini API with a fallback to local Ollama (llama3) if Gemini fails
    or if the API key is missing.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    
    # 1. Attempt Google Gemini
    if api_key and api_key.strip():
        try:
            logger.info("Path used: Google Gemini (gemini-2.5-flash)")
            client = genai.Client(api_key=api_key)
            
            config = types.GenerateContentConfig(
                temperature=0.2
            )
            # Adding system instruction if provided
            if system:
                config.system_instruction = system

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config
            )
            return response.text.strip()
            
        except Exception as e:
            logger.warning(f"Gemini API call failed ({e}). Falling back to Ollama...")
    else:
        logger.info("Path used: Fallback (GEMINI_API_KEY is empty)")

    # 2. Fallback to Ollama
    try:
        logger.info(f"Path used: Ollama (llama3) at {settings.OLLAMA_BASE_URL}")
        
        # Use httpx to call Ollama API
        url = f"{settings.OLLAMA_BASE_URL}/api/generate"
        payload = {
            "model": "llama3",
            "prompt": f"System: {system}\n\nUser: {prompt}" if system else prompt,
            "stream": False
        }
        
        response = httpx.post(url, json=payload, timeout=60.0)
        response.raise_for_status()
        
        result = response.json()
        return result.get("response", "")
        
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        return f"Error: Both Gemini and Ollama failed. Last error: {e}"
