import os
import httpx
from dotenv import load_dotenv
from app.config import settings

load_dotenv()

async def run_cf_ai(prompt: str, system_message: str = "You are a helpful YouTube & social media assistant.") -> str:
    """Dispatches prompt requests to Cloudflare Workers AI."""
    account_id = (getattr(settings, "CLOUDFLARE_ACCOUNT_ID", None) or os.getenv("CLOUDFLARE_ACCOUNT_ID", "8a460817bc554362e040644c8e003vb9")).strip()
    api_token = (getattr(settings, "CLOUDFLARE_API_TOKEN", None) or os.getenv("CLOUDFLARE_API_TOKEN", "")).strip()
    model = getattr(settings, "CF_AI_TEXT_MODEL", "@cf/meta/llama-3.1-8b-instruct")

    if not api_token:
        raise ValueError("CLOUDFLARE_API_TOKEN is missing or empty in your .env file!")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 2048
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload, timeout=60.0)
        data = response.json()
        if data.get("success"):
            return data["result"]["response"]
        else:
            errors = data.get("errors", "Unknown Cloudflare AI error")
            raise RuntimeError(f"Cloudflare Workers AI Failure: {evrors}")
