import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from knowledge import SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("mehran_ai")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
# Suppress httpx request logging to prevent URL/header leaks
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Environment (load BEFORE reading any setting below)
# ---------------------------------------------------------------------------
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# Per-call timeout and total budget for one /chat request.
GEMINI_TIMEOUT = _env_float("GEMINI_TIMEOUT_SECONDS", 10.0)
CONNECT_TIMEOUT = 4.0
REQUEST_DEADLINE = _env_float("REQUEST_DEADLINE_SECONDS", 25.0)
# Don't start a new attempt if less than this many seconds of budget remain.
MIN_ATTEMPT_WINDOW = 1.5
# How long a model is skipped after a 503 / timeout. 0 disables the cooldown.
MODEL_COOLDOWN = _env_float("MODEL_COOLDOWN_SECONDS", 75.0)
MAX_OUTPUT_TOKENS = _env_int("GEMINI_MAX_OUTPUT_TOKENS", 700)

# OpenAI path keeps a simple timeout.
TIMEOUT = httpx.Timeout(GEMINI_TIMEOUT, connect=CONNECT_TIMEOUT)

# ---------------------------------------------------------------------------
# Shared HTTP client (one TLS connection pool for the whole process)
# ---------------------------------------------------------------------------
_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )
    return _client


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


app = FastAPI(
    title="Mehran AI - Portfolio Assistant API",
    description="Backend service for Mehran Khan's portfolio AI assistant",
    version="0.2.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS Configuration
# ---------------------------------------------------------------------------
raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000,http://127.0.0.1:5500,https://mehran-sec.github.io",
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
# Maps client IP -> request timestamps within the last 60 seconds
ip_request_history: Dict[str, List[float]] = {}
_last_sweep = time.time()


def check_rate_limit(client_ip: str):
    global _last_sweep
    now = time.time()

    # Every 5 minutes, drop IPs that have gone quiet so the dict can't grow forever.
    if now - _last_sweep > 300:
        for ip in [ip for ip, ts in ip_request_history.items() if not ts or now - ts[-1] >= 60]:
            del ip_request_history[ip]
        _last_sweep = now

    timestamps = ip_request_history.get(client_ip, [])
    valid_timestamps = [ts for ts in timestamps if now - ts < 60]

    if len(valid_timestamps) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please wait a minute before asking another question.",
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
        description="The visitor's question about Mehran",
    )


class ChatResponse(BaseModel):
    answer: str
    # Additive fields: the existing widget ignores them, a newer one can use them.
    ok: bool = True
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# Gemini failover state (lives in this process; assumes a single worker)
# ---------------------------------------------------------------------------
# model -> unix time until which the model is skipped (503 / timeout / network)
model_down_until: Dict[str, float] = {}
# (model, key_index) -> unix time until which that key is skipped on that model (429 / auth)
key_down_until: Dict[Tuple[str, int], float] = {}
# models that rejected our thinking config; we stop sending it to them
thinking_unsupported: Set[str] = set()


@dataclass
class Outcome:
    kind: str  # ok | model_down | key_down | soft_fail | bad_request | thinking_rejected
    text: str = ""
    status: object = None
    cooldown: float = 0.0


def get_gemini_keys() -> List[str]:
    """Reads GEMINI_API_KEYS (comma-separated); falls back to the old GEMINI_API_KEY."""
    raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


if os.getenv("GEMINI_API_KEYS") and os.getenv("GEMINI_API_KEY"):
    logger.warning("Both GEMINI_API_KEYS and GEMINI_API_KEY are set; GEMINI_API_KEYS wins. Remove the one you don't use.")


def get_model_chain() -> List[str]:
    models: List[str] = []
    primary = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
    if primary:
        models.append(primary)
    for fm in os.getenv("GEMINI_FALLBACK_MODEL", "").split(","):
        fm = fm.strip()
        if fm and fm not in models:
            models.append(fm)
    return models or ["gemini-3.1-flash-lite"]


def build_gemini_body(user_message: str, model: str) -> dict:
    gen_config: dict = {"temperature": 0.2, "maxOutputTokens": MAX_OUTPUT_TOKENS}
    # Opt-in thinking control. Field names differ by model generation, so nothing is sent unless
    # you set GEMINI_THINKING_LEVEL (Gemini 3 style) or GEMINI_THINKING_BUDGET (2.5 style).
    if model not in thinking_unsupported:
        level = os.getenv("GEMINI_THINKING_LEVEL", "").strip()
        budget = os.getenv("GEMINI_THINKING_BUDGET", "").strip()
        if level:
            gen_config["thinkingConfig"] = {"thinkingLevel": level}
        elif budget.lstrip("-").isdigit():
            gen_config["thinkingConfig"] = {"thinkingBudget": int(budget)}
    return {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": gen_config,
    }


def _retry_after_seconds(response: httpx.Response, default: float = 60.0) -> float:
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return max(1.0, min(float(raw), 900.0))
        except ValueError:
            pass
    # Daily quota won't recover in a minute; bench the key longer.
    if "perday" in response.text.lower():
        return 900.0
    return default


async def _gemini_attempt(model: str, key: str, body: dict, timeout_s: float) -> Outcome:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    try:
        response = await get_client().post(
            url,
            headers=headers,
            json=body,
            timeout=httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT, timeout_s)),
        )
    except httpx.TimeoutException as exc:
        return Outcome("model_down", status="timeout", cooldown=MODEL_COOLDOWN, text=type(exc).__name__)
    except httpx.RequestError as exc:
        return Outcome("model_down", status="network", cooldown=0.0, text=type(exc).__name__)
    except Exception as exc:  # never let one bad attempt crash the request
        return Outcome("soft_fail", status="error", text=type(exc).__name__)

    code = response.status_code

    if code == 200:
        try:
            data = response.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(
                p["text"] for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")
            ).strip()
        except (ValueError, KeyError, IndexError, TypeError):
            return Outcome("soft_fail", status="unparseable")
        if not text:
            return Outcome("soft_fail", status="empty")
        return Outcome("ok", text=text)

    if code == 429:
        return Outcome("key_down", status=429, cooldown=_retry_after_seconds(response))
    if code in (401, 403):
        return Outcome("key_down", status=code, cooldown=600.0)
    if code in (500, 502, 503, 504):
        return Outcome("model_down", status=code, cooldown=MODEL_COOLDOWN)
    if code == 400:
        if "thinking" in response.text.lower() and "thinkingConfig" in body.get("generationConfig", {}):
            return Outcome("thinking_rejected", status=400)
        return Outcome("bad_request", status=400, text=response.text[:200])
    return Outcome("soft_fail", status=code)


def _failure_message(last_status: object, deadline_hit: bool) -> str:
    if deadline_hit or last_status == "timeout":
        return "The AI service is responding slowly right now. Please try again in a moment."
    if last_status == 429:
        return "The AI service is busy right now. Please try again shortly."
    if last_status == "network":
        return "A network error occurred while contacting the AI service. Please try again."
    return "The AI service is temporarily unavailable. Please try again in a moment."


async def query_gemini(user_message: str, api_keys: List[str]) -> Tuple[str, bool, Optional[str]]:
    """Returns (answer, ok, model_used).

    Rules:
      * 503 / timeout / network error -> the MODEL is the problem: bench it, go to the next model.
      * 429 / 401 / 403               -> the KEY is the problem: bench that key on that model, try the next key.
      * empty or unparseable answer   -> next model, no benching.
      * Models and keys that are benched are skipped, but tried as a last resort so we never return nothing.
      * The whole request is capped by REQUEST_DEADLINE.
    """
    models = get_model_chain()
    deadline = time.monotonic() + REQUEST_DEADLINE
    tried: Set[Tuple[str, int]] = set()
    failed_this_request: Set[str] = set()
    last_status: object = None
    attempts = 0
    deadline_hit = False

    for strict in (True, False):  # strict pass honours cooldowns; lenient pass is the last resort
        for model in models:
            if model in failed_this_request:
                continue
            if strict and model_down_until.get(model, 0.0) > time.time():
                continue

            for key_index, key in enumerate(api_keys, start=1):
                if (model, key_index) in tried:
                    continue
                if strict and key_down_until.get((model, key_index), 0.0) > time.time():
                    continue

                remaining = deadline - time.monotonic()
                if remaining < MIN_ATTEMPT_WINDOW:
                    deadline_hit = True
                    break

                tried.add((model, key_index))
                attempts += 1
                timeout_s = min(GEMINI_TIMEOUT, remaining)
                outcome = await _gemini_attempt(model, key, build_gemini_body(user_message, model), timeout_s)

                if outcome.kind == "thinking_rejected":
                    thinking_unsupported.add(model)
                    logger.warning(f"Gemini {model} rejected thinkingConfig; retrying without it.")
                    remaining = deadline - time.monotonic()
                    if remaining < MIN_ATTEMPT_WINDOW:
                        deadline_hit = True
                        break
                    outcome = await _gemini_attempt(
                        model, key, build_gemini_body(user_message, model), min(GEMINI_TIMEOUT, remaining)
                    )

                if outcome.kind == "ok":
                    logger.info(f"Gemini answered: model={model} key#{key_index} attempts={attempts}")
                    return outcome.text, True, model

                last_status = outcome.status
                logger.warning(
                    f"Gemini {model} key#{key_index} -> {outcome.kind} ({outcome.status}) {outcome.text}".rstrip()
                )

                if outcome.kind == "key_down":
                    key_down_until[(model, key_index)] = time.time() + outcome.cooldown
                    continue  # try the next key on the same model

                # model_down / soft_fail / bad_request: another key on this model won't help
                if outcome.kind == "model_down":
                    failed_this_request.add(model)
                    if outcome.cooldown > 0:
                        model_down_until[model] = time.time() + outcome.cooldown
                break

            if deadline_hit:
                break
        if deadline_hit:
            break

    logger.error(f"All Gemini attempts failed: attempts={attempts} last_status={last_status} deadline_hit={deadline_hit}")
    return _failure_message(last_status, deadline_hit), False, None


async def query_openai(user_message: str, api_key: str) -> Tuple[str, bool, Optional[str]]:
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    url = "https://api.openai.com/v1/chat/completions"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            response = await get_client().post(url, headers=headers, json=payload, timeout=TIMEOUT)
            response.raise_for_status()

            data = response.json()
            choices = data.get("choices")
            if not choices or not isinstance(choices, list):
                logger.warning("OpenAI response missing or empty 'choices' field")
                return "I apologize, but I could not parse the response from the AI model.", False, None

            text = choices[0].get("message", {}).get("content", "").strip()
            if not text:
                return "I apologize, but I received an empty response from the AI model.", False, None
            return text, True, model

        except httpx.TimeoutException as exc:
            logger.warning(f"OpenAI API timeout on attempt {attempt}/{max_attempts}: {type(exc).__name__}")
            if attempt < max_attempts:
                continue
            return "The AI service is responding slowly right now. Please try again in a moment.", False, None

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            logger.error(f"OpenAI API HTTPStatusError: status_code={status_code}")
            return "The AI service is temporarily unavailable. Please try again in a moment.", False, None

        except httpx.RequestError as exc:
            logger.error(f"OpenAI API request error: {type(exc).__name__}")
            return "A network error occurred while contacting the AI service. Please try again.", False, None

        except Exception as exc:
            logger.error(f"Unexpected error in query_openai: {type(exc).__name__}")
            return "An unexpected error occurred while generating the response. Please try again.", False, None

    return "The AI service is responding slowly right now. Please try again in a moment.", False, None


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.api_route("/health", methods=["GET", "HEAD"], summary="Health check endpoint")
async def health():
    # Minimal on purpose: used by uptime pingers (GET or HEAD) and the site's wake-up check.
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse, summary="Chat with Mehran AI")
async def chat(request: Request, payload: ChatRequest):
    started = time.monotonic()

    # 1. Input sanitization
    user_msg = payload.message.strip()
    if not user_msg:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Question cannot be blank.")

    # 2. Rate limiting check
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    # 3. Detect and route to configured LLM
    gemini_keys = get_gemini_keys()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    preferred_provider = os.getenv("LLM_PROVIDER", "gemini").lower()

    if preferred_provider == "openai" and openai_key:
        answer, ok, model_used = await query_openai(user_msg, openai_key)
    elif gemini_keys:
        answer, ok, model_used = await query_gemini(user_msg, gemini_keys)
    elif openai_key:
        answer, ok, model_used = await query_openai(user_msg, openai_key)
    else:
        answer, ok, model_used = (
            "Mehran AI backend is running, but no LLM API key has been configured yet. "
            "Please add GEMINI_API_KEY or OPENAI_API_KEY to your environment.",
            False,
            None,
        )

    logger.info(f"chat ok={ok} model={model_used} total_ms={int((time.monotonic() - started) * 1000)}")
    return ChatResponse(answer=answer, ok=ok, model=model_used)
