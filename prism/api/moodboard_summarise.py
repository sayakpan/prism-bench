"""
POST /api/ai/moodboard-summarise

Rolling Haiku digest of chat history for thread rehydration.
Returns { summary, generatedAt }. Never errors out â€” falls back to a
generic line if the Haiku call fails or the key is missing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from anthropic import AsyncAnthropic
from fastapi import APIRouter
from pydantic import BaseModel

from moodboard_ai.config import get_settings
from moodboard_ai.log import step

router = APIRouter()

_SUMMARY_FALLBACK = (
    "User has been iterating on this moodboard; no detailed summary available."
)

# Cap how much chat history we feed the summariser. Older messages are
# folded into existingSummary on the next refresh, so nothing is lost â€”
# we just don't pay tokens to re-process them every time.
_MAX_TURNS_PER_REFRESH = 24
_MAX_TEXT_PER_MESSAGE = 800

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic | None:
    global _client
    if _client is not None:
        return _client
    key = get_settings().anthropic_api_key
    if not key:
        return None
    _client = AsyncAnthropic(api_key=key)
    return _client


def _render_messages(messages: list[dict[str, Any]] | None) -> str:
    if not isinstance(messages, list):
        return ""
    trimmed = messages[-_MAX_TURNS_PER_REFRESH:]
    lines: list[str] = []
    for i, m in enumerate(trimmed):
        role_in = (m or {}).get("role")
        role = (
            "Assistant" if role_in == "assistant"
            else ("User" if role_in == "user" else "Note")
        )
        text = ((m or {}).get("text") or "").strip()[:_MAX_TEXT_PER_MESSAGE]
        if not text:
            continue
        lines.append(f"{i + 1}. {role}: {text}")
    return "\n".join(lines)


def _now_iso_z() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class SummariseRequest(BaseModel):
    messages: list[dict[str, Any]] | None = None
    existingSummary: str | None = None


@router.post("/")
async def summarise(body: SummariseRequest) -> dict[str, Any]:
    client = _get_client()
    existing_clean = (body.existingSummary or "").strip()

    if client is None or not body.messages:
        return {
            "summary": existing_clean or _SUMMARY_FALLBACK,
            "generatedAt": _now_iso_z(),
        }

    t = step("haiku", "summariseConversation", {
        "msgs": len(body.messages),
        "hasExisting": bool(existing_clean),
    })

    transcript = _render_messages(body.messages)
    existing_block = (
        "Existing summary (from earlier turns; preserve any still-relevant decisions):\n"
        + existing_clean[:3000]
        if existing_clean
        else "No existing summary."
    )

    system_prompt = " ".join([
        "You compress fashion-moodboard chat histories into a 200-400 token rolling digest.",
        "The digest is used as system context when a user iterates on a moodboard whose conversation state has expired upstream.",
        "Preserve: fabric / colour / styling decisions, the user's stated preferences, any explicit do-not-do rules, the current creative direction.",
        "Discard: pleasantries, redundant restatements, anything superseded by a later turn.",
        "Write in neutral past tense, third-person ('The user requestedâ€¦', 'The AI generatedâ€¦'). No bullet points, no markdown, plain prose.",
        "If the existing summary already covers most history, integrate the new turns into it rather than rewriting from scratch.",
    ])

    user_msg = "\n\n".join([
        existing_block,
        f"Chat history (most recent {min(len(body.messages), _MAX_TURNS_PER_REFRESH)} turns):",
        transcript,
        "Produce the updated digest now. 200-400 tokens.",
    ])

    try:
        result = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            temperature=0.4,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as err:
        t.fail(err)
        return {
            "summary": existing_clean or _SUMMARY_FALLBACK,
            "generatedAt": _now_iso_z(),
        }

    text = "".join(
        getattr(b, "text", "") for b in (result.content or [])
        if getattr(b, "type", None) == "text"
    ).strip()

    if not text:
        t.done({"used": "fallback"})
        return {
            "summary": existing_clean or _SUMMARY_FALLBACK,
            "generatedAt": _now_iso_z(),
        }

    t.done({"chars": len(text)})
    return {"summary": text, "generatedAt": _now_iso_z()}
