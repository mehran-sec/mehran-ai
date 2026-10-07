# Mehran AI — Portfolio Assistant Backend (V0)

Lightweight FastAPI backend powering the portfolio assistant for [mehran-sec.github.io](https://mehran-sec.github.io/).

---

## Architecture (Phase 1)

```
Client (curl / browser)
       │
       ▼
 FastAPI Backend (/chat)
       │
       ├── Input validation & sanitization
       ├── Rate limiting (10 req/min per IP)
       ├── System Prompt + Mehran Knowledge Base
       │
       ▼
   LLM API (Gemini / OpenAI)
       │
       ▼
JSON Response ({"answer": "..."})
```

---

## Quickstart (Local Setup)

### 1. Create a virtual environment & install dependencies

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure your API key

Edit `.env`:
```bash
# Get a free API key at https://aistudio.google.com/
GEMINI_API_KEY=your_actual_key_here
```

### 3. Start the server

```bash
uvicorn main:app --reload --port 8000
```

### 4. Test the endpoints

**Health Check:**
```bash
curl http://localhost:8000/health
```

**Chat Query:**
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What does Mehran know about SOC?"}'
```
