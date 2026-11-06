import os
import logging
from google import genai
from google.genai import types

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def call_llm(prompt: str, system: str = '') -> str:
    """
    Calls Google Gemini API. Swapped out Claude & Ollama logic for local deployment.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not found in environment.")
        return "Error: GEMINI_API_KEY is missing."

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
        logger.error(f"Gemini API call failed: {e}")
        return f"Error: Gemini API call failed: {e}"
