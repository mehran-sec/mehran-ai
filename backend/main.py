import os
import time
from typing import Dict, List
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from dotenv import load_dotenv

from knowledge import SYSTEM_PROMPT

# Load environment variables from .env
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
async def query_gemini(user_message: str, api_key: str) -> str:
    model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_message}]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 700
        }
    }
    
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)
        
        if response.status_code != 200:
            error_detail = response.text
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Gemini API returned an error ({response.status_code}): {error_detail}"
            )
            
        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            return "I apologize, but I could not parse the response from the AI model."

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
    
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            error_detail = response.text
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"OpenAI API returned an error ({response.status_code}): {error_detail}"
            )
            
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            return "I apologize, but I could not parse the response from the AI model."

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", summary="Health check endpoint")
async def health():
    provider = os.getenv("LLM_PROVIDER", "auto")
    has_gemini = bool(os.getenv("GEMINI_API_KEY"))
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
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    preferred_provider = os.getenv("LLM_PROVIDER", "gemini").lower()

    if preferred_provider == "openai" and openai_key:
        answer = await query_openai(user_msg, openai_key)
    elif gemini_key:
        answer = await query_gemini(user_msg, gemini_key)
    elif openai_key:
        answer = await query_openai(user_msg, openai_key)
    else:
        # Development fallback message if no API key is set yet
        answer = (
            "Mehran AI backend is running, but no LLM API key has been configured yet. "
            "Please add GEMINI_API_KEY or OPENAI_API_KEY to your .env file."
        )

    return ChatResponse(answer=answer)
