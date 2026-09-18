"""
POST /api/ai/suggest-styles          (house prefix, matches the other AI routes)
POST /v1/moodboard/suggest-styles    (alias — the path named in the FE spec)

AI style auto-suggest. Replaces the client-side random pick behind
*PSL Styles -> AI Auto Suggest* in the moodboard wizard: instead of shuffling
the matched-garment list and taking N, Claude ranks the candidates against the
moodboard brief and returns N picks with a user-facing reason each.

Request:
  {
    count: 1..5,
    season: "AW25",
    gender: ["Women"],                  (Context step "Category", multi-select)
    style_category: ["Dresses","Tops"], (Context step "Style Category")
    user_vision: "...",                 (raw brief text; may be empty)
    candidates: [ {...} ],              (verbatim top_matches from
                                         get_matching_garments — the ONLY
                                         garments that may be returned)
    stream: true                        (default; false -> one JSON body)
  }

SSE events (order: meta -> (status|ping)* -> pick x N -> final, or error):
  meta   -> { model, candidate_count, requested }
  status -> { stage, message }
  pick   -> { rank, id, gsr_no, garment_name, confidence, reason, factors }
  ping   -> {}                                    (every ~10s while thinking)
  final  -> { picks: [id], summary, runners_up, model, elapsed_ms }
  error  -> { error, code, retriable }

`stream: false` returns 200 application/json with the `final` payload, except
`picks` is the full array of pick objects rather than just ids.

Validation failures are a plain JSON body before the stream opens (400/500);
once the stream is open, failures are an `error` event, never a bare
disconnect. On any error the FE falls back to its random pick, so the board
still generates — it just loses the reasons.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from typing import Any

import anthropic
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.suggest_styles import (
    MAX_COUNT,
    MODEL,
    normalise_candidates,
    run_suggest_styles,
)

router = APIRouter()

# Cadence of the keepalive `ping` events, so a proxy doesn't drop the
# connection while Claude is still scoring.
_PING_SECONDS = 10.0


class SuggestStylesRequest(BaseModel):
    count: int | None = None
    season: str | None = None
    gender: list[str] | str | None = None
    style_category: list[str] | str | None = None
    user_vision: str | None = None
    candidates: list[dict[str, Any]] | None = None
    stream: bool | None = None


def _error_body(message: str, code: str) -> dict[str, str]:
    return {"error": message, "code": code}


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


def _classify(err: Exception) -> tuple[str, str, bool]:
    """(message, code, retriable) for the SSE `error` event."""
    if isinstance(err, anthropic.RateLimitError):
        return ("Rate limited upstream. Try again shortly.", "RATE_LIMITED", True)
    if isinstance(err, anthropic.APIConnectionError):
        return ("Could not reach the model provider.", "UPSTREAM_FAILED", True)
    if isinstance(err, anthropic.APIStatusError):
        return (
            f"Model provider error ({err.status_code}).",
            "UPSTREAM_FAILED",
            err.status_code >= 500,
        )
    msg = str(err) or err.__class__.__name__
    lower = msg.lower()
    aborted = "aborted" in lower or "disconnected" in lower or "cancel" in lower
    return (msg, "UPSTREAM_FAILED", not aborted)


@router.post("")
@router.post("/")
async def suggest_styles(req: Request, body: SuggestStylesRequest):
    # ── Pre-stream validation (plain JSON envelopes) ──────────────────
    candidates = normalise_candidates(body.candidates)
    if not candidates:
        return JSONResponse(
            status_code=400,
            content=_error_body("Candidate pool empty", "NO_CANDIDATES"),
        )

    count = body.count if isinstance(body.count, int) else 3
    if count < 1 or count > len(candidates) or count > MAX_COUNT:
        return JSONResponse(
            status_code=400,
            content=_error_body(
                f"count must be between 1 and "
                f"{min(MAX_COUNT, len(candidates))} for this candidate pool",
                "INVALID_COUNT",
            ),
        )

    if not get_settings().anthropic_api_key:
        return JSONResponse(
            status_code=500,
            content=_error_body(
                "ANTHROPIC_API_KEY is not configured.", "UPSTREAM_FAILED"
            ),
        )

    season = (body.season or "").strip()
    gender = _str_list(body.gender)
    style_category = _str_list(body.style_category)
    user_vision = (body.user_vision or "").strip()

    kwargs: dict[str, Any] = {
        "count": count,
        "season": season,
        "gender": gender,
        "style_category": style_category,
        "user_vision": user_vision,
        "candidates": candidates,
    }

    t_req = step("ai-suggest-styles", "request", {
        "count": count,
        "candidates": len(candidates),
        "stream": body.stream is not False,
    })

    # ── Non-streaming fallback ────────────────────────────────────────
    if body.stream is False:
        try:
            result = await run_suggest_styles(**kwargs)
        except Exception as err:
            message, code, retriable = _classify(err)
            t_req.fail(message)
            status = 429 if code == "RATE_LIMITED" else 500
            headers = {"Retry-After": "30"} if status == 429 else None
            return JSONResponse(
                status_code=status,
                content={**_error_body(message, code), "retriable": retriable},
                headers=headers,
            )
        t_req.done({"picks": len(result["picks"])})
        return result

    # ── SSE ───────────────────────────────────────────────────────────
    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        def emit(event: str, data: dict[str, Any]) -> dict[str, Any]:
            return {"event": event, "data": json.dumps(data, ensure_ascii=False)}

        yield emit("meta", {
            "model": MODEL,
            "candidate_count": len(candidates),
            "requested": count,
        })
        yield emit("status", {
            "stage": "scoring",
            "message": (
                f"Weighing {len(candidates)} garment"
                f"{'' if len(candidates) == 1 else 's'} against the "
                f"{season or 'moodboard'} brief…"
            ),
        })

        def on_event_cb(ev: dict[str, Any]) -> None:
            try:
                event_queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass

        async def run() -> None:
            try:
                result = await run_suggest_styles(**kwargs, on_event=on_event_cb)
                event_queue.put_nowait({"type": "__final__", **result})
            except asyncio.CancelledError:
                raise
            except Exception as err:
                message, code, retriable = _classify(err)
                event_queue.put_nowait({
                    "type": "__error__",
                    "error": message,
                    "code": code,
                    "retriable": retriable,
                })
            finally:
                event_queue.put_nowait(None)

        run_task = asyncio.create_task(run())
        last_ping = time.monotonic()

        try:
            while True:
                if await req.is_disconnected():
                    run_task.cancel()
                    return

                try:
                    ev = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - last_ping >= _PING_SECONDS:
                        last_ping = now
                        yield emit("ping", {})
                    continue

                if ev is None:
                    break

                last_ping = time.monotonic()
                etype = ev.get("type")

                if etype == "__final__":
                    yield emit("final", {
                        "picks": [p["id"] for p in ev.get("picks") or []],
                        "summary": ev.get("summary"),
                        "runners_up": ev.get("runners_up") or [],
                        "model": ev.get("model"),
                        "elapsed_ms": ev.get("elapsed_ms"),
                    })
                    t_req.done({
                        "picks": len(ev.get("picks") or []),
                        "elapsedMs": ev.get("elapsed_ms"),
                    })
                elif etype == "__error__":
                    t_req.fail(ev.get("error"))
                    yield emit("error", {
                        "error": ev.get("error") or "Style suggestion failed",
                        "code": ev.get("code") or "UPSTREAM_FAILED",
                        "retriable": bool(ev.get("retriable")),
                    })
                else:
                    payload = {k: v for k, v in ev.items() if k != "type"}
                    yield emit(etype or "message", payload)
        except asyncio.CancelledError:
            run_task.cancel()
            raise

    return EventSourceResponse(event_generator(), ping=15)
