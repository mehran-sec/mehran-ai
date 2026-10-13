import asyncio
import logging
import os
import time
from typing import Dict, List
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from dotenv import load_dotenv

from knowledge import SYSTEM_PROMPT

# Configure logging
logger = logging.getLogger("mehran_ai")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
# Suppress httpx request logging to prevent URL/header leaks
logging.getLogger("httpx").setLevel(logging.WARNING)

# Explicit timeout configuration (10s total/read, 4s connect for fast failover)
TIMEOUT = httpx.Timeout(10.0, connect=4.0)

# Load environment variables from .env (checks script directory then cwd)
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()

app = FastAPI(
    title="Mehran AI - Portfolio Assistant API",
    description="Backend service for Mehran Khan's portfolio AI assistant",
    version="0.1.0"
)

# ---------------------------------------------------------------------------
# CORS Configuration
# ---------------------------------------------------------------------------
raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000,http://127.0.0.1:5500,https://mehran-sec.github.io"
)
allowed_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-Memory Rate Limiting
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))
# Maps client IP -> list of request timestamps within the last 60 seconds
ip_request_history: Dict[str, List[float]] = {}

def check_rate_limit(client_ip: str):
    now = time.time()
    timestamps = ip_request_history.get(client_ip, [])
    # Filter out requests older than 60 seconds
    valid_timestamps = [ts for ts in timestamps if now - ts < 60]
    
    if len(valid_timestamps) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please wait a minute before asking another question."
        )
    
    valid_timestamps.append(now)
    ip_request_history[client_ip] = valid_timestamps

# ---------------------------------------------------------------------------
# Request & Response Models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="The visitor's question about Mehran"
    )

class ChatResponse(BaseModel):
    answer: str

# ---------------------------------------------------------------------------
# LLM Providers Logic (Gemini & OpenAI)
# ---------------------------------------------------------------------------
RETRYABLE = {429, 500, 502, 503, 504}

async def query_gemini(user_message: str, api_keys: List[str]) -> str:
    """Try each model with each API key. Fast failover on overload (503), quota (429), or timeout."""
    models = []
    primary_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
    if primary_model:
        models.append(primary_model)
    fallback_models_raw = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
    for fm in [m.strip() for m in fallback_models_raw.split(",") if m.strip()]:
        if fm not in models:
            models.append(fm)
    if not models:
        models = ["gemini-3.1-flash-lite"]

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
    }

    last_status = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        model_overloaded = False
        for key_index, key in enumerate(api_keys, start=1):
            if model_overloaded:
                break  # Overloaded model: skip remaining keys on this model, jump to fallback model
            headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    response = await client.post(url, headers=headers, json=payload)

                if response.status_code == 503:
                    last_status = 503
                    logger.warning(f"Gemini {model} key#{key_index} -> HTTP 503 Overloaded. Immediately switching to fallback model.")
                    model_overloaded = True
                    break

                if response.status_code == 429:
                    last_status = 429
                    logger.warning(f"Gemini {model} key#{key_index} -> HTTP 429 Quota Exceeded. Trying next key.")
                    continue  # Quota is per-key: immediately try next key

                if response.status_code in RETRYABLE:
                    last_status = response.status_code
                    logger.warning(f"Gemini {model} key#{key_index} -> HTTP {response.status_code}")
                    continue

                response.raise_for_status()
                data = response.json()
                candidates = data.get("candidates")
                if not candidates or not isinstance(candidates, list):
                    logger.warning("Gemini response missing or empty 'candidates' field")
                    return "I apologize, but I could not parse the response from the AI model."

                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts or not isinstance(parts, list):
                    logger.warning("Gemini candidate missing 'content.parts' field")
                    return "I apologize, but I could not parse the response from the AI model."

                text = parts[0].get("text", "").strip()
                if not text:
                    return "I apologize, but I received an empty response from the AI model."
                return text

            except httpx.TimeoutException as exc:
                last_status = "timeout"
                logger.warning(f"Gemini API timeout ({model}, key#{key_index}): {type(exc).__name__}. Switching to next key/model.")
                continue
            except httpx.HTTPStatusError as exc:
                last_status = exc.response.status_code
                logger.error(f"Gemini API HTTPStatusError ({model}, key#{key_index}): status_code={last_status}, detail={exc.response.text}")
                continue
            except httpx.RequestError as exc:
                last_status = "network"
                logger.error(f"Gemini API request error ({model}, key#{key_index}): {type(exc).__name__}")
                continue
            except Exception as exc:
                logger.error(f"Unexpected error in query_gemini: {type(exc).__name__}")
                return "An unexpected error occurred while generating the response. Please try again."

    logger.error(f"All Gemini models/keys failed. Last status: {last_status}")
    if last_status == "timeout":
        return "The AI service took too long to respond. Please try again."
    if last_status == "network":
        return "A network error occurred while contacting the AI service. Please try again."
    return f"The AI service returned an error (HTTP {last_status}). Please try again later."

async def query_openai(user_message: str, api_key: str) -> str:
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    url = "https://api.openai.com/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]
    }
    
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                
                data = response.json()
                choices = data.get("choices")
                if not choices or not isinstance(choices, list):
                    logger.warning("OpenAI response missing or empty 'choices' field")
                    return "I apologize, but I could not parse the response from the AI model."
                
                text = choices[0].get("message", {}).get("content", "").strip()
                if not text:
                    return "I apologize, but I received an empty response from the AI model."
                return text

        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TimeoutException) as exc:
            logger.warning(f"OpenAI API timeout on attempt {attempt}/{max_attempts}: {type(exc).__name__}")
            if attempt < max_attempts:
                continue
            return "The AI service took too long to respond. Please try again."

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            logger.error(f"OpenAI API HTTPStatusError: status_code={status_code}")
            return f"The AI service returned an error (HTTP {status_code}). Please try again later."

        except httpx.RequestError as exc:
            logger.error(f"OpenAI API request error: {type(exc).__name__}")
            return "A network error occurred while contacting the AI service. Please try again."

        except Exception as exc:
            logger.error(f"Unexpected error in query_openai: {type(exc).__name__}")
            return "An unexpected error occurred while generating the response. Please try again."

    return "The AI service took too long to respond. Please try again."

def get_gemini_keys() -> List[str]:
    """Reads GEMINI_API_KEYS (comma-separated); falls back to the old GEMINI_API_KEY."""
    raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", summary="Health check endpoint")
async def health():
    provider = os.getenv("LLM_PROVIDER", "auto")
    has_gemini = bool(get_gemini_keys())
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    return {
        "status": "ok",
        "service": "Mehran AI Backend",
        "llm_configured": has_gemini or has_openai,
        "active_provider": "gemini" if has_gemini else ("openai" if has_openai else "none")
    }

@app.post("/chat", response_model=ChatResponse, summary="Chat with Mehran AI")
async def chat(request: Request, payload: ChatRequest):
    # 1. Input sanitization
    user_msg = payload.message.strip()
    if not user_msg:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question cannot be blank."
        )

    # 2. Rate limiting check
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    # 3. Detect and route to configured LLM
    gemini_keys = get_gemini_keys()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    preferred_provider = os.getenv("LLM_PROVIDER", "gemini").lower()

    if preferred_provider == "openai" and openai_key:
        answer = await query_openai(user_msg, openai_key)
    elif gemini_keys:
        answer = await query_gemini(user_msg, gemini_keys)
    elif openai_key:
        answer = await query_openai(user_msg, openai_key)
    else:
        # Development fallback message if no API key is set yet
        answer = (
            "Mehran AI backend is running, but no LLM API key has been configured yet. "
            "Please add GEMINI_API_KEY or OPENAI_API_KEY to your .env file."
        )

    return ChatResponse(answer=answer)
