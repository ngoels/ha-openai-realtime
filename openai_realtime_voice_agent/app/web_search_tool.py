"""Web search function-tool.

Lets the Realtime assistant look things up online (weather, news, facts, opening
hours, prices, recent events). The Realtime API has NO native web search, and
pipecat 0.0.97 only supports custom *function* tools — so this is wired exactly
like the disconnect tool: a `web_search` function tool whose handler runs a
SECOND, server-side OpenAI call (the Responses API `web_search` built-in tool),
then returns a short, spoken-friendly answer the Realtime model reads aloud.

Uses the add-on's existing OPENAI_API_KEY — no extra account/vendor. Default
model gpt-5.5 gives the best-quality search answers; it's configurable via
WEB_SEARCH_MODEL (the mini/nano models are cheaper) so a different price/quality
(or a renamed model) can be swapped in without a code change.
"""
import logging
from typing import Dict, Any, Callable, Awaitable, Optional, TYPE_CHECKING

import httpx
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)


def get_web_search_tool_definition() -> Dict[str, Any]:
    """OpenAI Realtime function-tool definition for web search."""
    return {
        "type": "function",
        "name": "web_search",
        "description": (
            "Search the public internet for current, real-time, or factual "
            "information the assistant does not already know — for example the "
            "weather, news, sports scores, opening hours, prices, travel info, or "
            "recent events. Do NOT use this for controlling the smart home — use "
            "the Hass* tools for lights, switches, climate, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query, phrased as a clear natural-language "
                        "question in the user's language."
                    ),
                }
            },
            "required": ["query"],
        },
    }


async def resolve_home_location(
    ha_mcp_url: str,
    access_token: Optional[str],
    city: str = "",
    region: str = "",
) -> Optional[Dict[str, str]]:
    """Build the web_search `user_location` for the home.

    Country and time zone come from Home Assistant's own settings
    (Settings -> System -> General), read once via the core REST API
    (`/api/config`, next to the MCP endpoint). City and region come from the
    add-on options, because Home Assistant stores only coordinates and a free
    home name, not a city. Without this, OpenAI's search assumes the US for
    questions like "weather tomorrow" that name no place.
    Returns None when nothing is known (search then runs without a location).
    """
    location: Dict[str, str] = {"type": "approximate"}
    if access_token and ha_mcp_url:
        base = ha_mcp_url.rstrip("/")
        if base.endswith("/mcp"):
            base = base[: -len("/mcp")]
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{base}/config",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                resp.raise_for_status()
                cfg = resp.json()
            country = str(cfg.get("country") or "").strip().upper()
            if len(country) == 2:
                location["country"] = country
            time_zone = str(cfg.get("time_zone") or "").strip()
            if time_zone:
                location["timezone"] = time_zone
        except Exception as e:
            logger.warning(f"⚠️ Could not read the home location from Home Assistant: {e!r}")
    if city.strip():
        location["city"] = city.strip()
    if region.strip():
        location["region"] = region.strip()
    return location if len(location) > 1 else None


def create_web_search_tool_handler(
    api_key: str, model: str, user_location: Optional[Dict[str, str]] = None
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    """Create a web_search handler for pipecat's OpenAIRealtimeLLMService.

    The handler calls the OpenAI Responses API with the built-in `web_search`
    tool and returns a short answer via ``params.result_callback`` (which becomes
    the function_call_output the Realtime model speaks).
    """
    client = AsyncOpenAI(api_key=api_key)
    search_tool: Dict[str, Any] = {"type": "web_search"}
    if user_location:
        search_tool["user_location"] = user_location

    async def web_search_tool_handler(params: "FunctionCallParams") -> None:
        query = (params.arguments or {}).get("query", "").strip()
        logger.info(f"🔎 web_search called: {query!r} (model={model})")

        if not query:
            # Status texts are English on purpose: they go back to the Realtime
            # model, which says them in the conversation's language.
            await params.result_callback("No search query was given.")
            return

        try:
            response = await client.responses.create(
                model=model,
                tools=[search_tool],
                input=(
                    "Answer in at most 2 short sentences suitable for being read "
                    "aloud, in the same language as the question. Do not include "
                    "URLs, citations, or markdown. Question: " + query
                ),
            )
            answer = (getattr(response, "output_text", None) or "").strip()
            logger.info(f"🔎 web_search answer: {answer[:200]}")
            await params.result_callback(answer or "Nothing about this could be found online.")
        except Exception as e:
            logger.error(f"❌ web_search failed: {e}", exc_info=True)
            await params.result_callback("The web search failed just now.")

    return web_search_tool_handler
