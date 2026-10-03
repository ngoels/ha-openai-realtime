"""MCP service integration using Pipecat's MCPClient with StreamableHTTP."""
import asyncio
import json
import logging
from typing import Optional
from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters

logger = logging.getLogger(__name__)


class HomeAssistantMCPService:
    """Home Assistant MCP service using Pipecat's MCPClient."""
    
    def __init__(self, url: str, access_token: str):
        """
        Initialize Home Assistant MCP service.
        
        Args:
            url: Home Assistant MCP Server URL (e.g., http://supervisor/core/api/mcp)
            access_token: Long-lived access token for Home Assistant
        """
        self.url = url
        self.access_token = access_token
        self.mcp_client: Optional[MCPClient] = None
        
    async def initialize(self) -> MCPClient:
        """Initialize and return the MCP client."""
        try:
            logger.info(f"🔗 Initializing Home Assistant MCP Client at {self.url}")
            
            # Create StreamableHTTP parameters with authentication
            server_params = StreamableHttpParameters(
                url=self.url,
                headers={
                    "Authorization": f"Bearer {self.access_token}"
                }
            )
            
            # Create MCP client
            self.mcp_client = MCPClient(server_params=server_params)
            
            logger.info("✅ Home Assistant MCP Client initialized")
            return self.mcp_client
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise
    
    def get_client(self) -> Optional[MCPClient]:
        """Get the MCP client instance."""
        return self.mcp_client

    async def fetch_entity_overview(self, timeout_s: float = 10.0) -> Optional[str]:
        """Return a names-only overview of the exposed entities, or None on failure.

        Home Assistant's own conversation agents put the exposed-entity overview
        (the GetLiveContext output) into the system prompt, so the model knows the
        exact entity and area names before it calls a tool. Over MCP the model
        only gets tool definitions and has to *guess* GetLiveContext filters,
        which misses badly when the user's language differs from the entity
        names. Calling GetLiveContext once here restores that behaviour.

        Live values (state + attributes) are stripped: a realtime session lives up
        to an hour, so they'd go stale and the model would answer from them. The
        model keeps calling GetLiveContext for current values, now with the
        correct names.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _fetch() -> Optional[str]:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            async with streamablehttp_client(self.url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    name = next(
                        (t.name for t in tools.tools if t.name.endswith("GetLiveContext")),
                        None,
                    )
                    if not name:
                        logger.warning("⚠️ MCP server offers no GetLiveContext tool; no entity overview")
                        return None
                    result = await session.call_tool(name, {})
                    text = "".join(
                        getattr(c, "text", "") for c in result.content
                        if getattr(c, "type", "") == "text"
                    )
            return _extract_overview(text)

        try:
            return await asyncio.wait_for(_fetch(), timeout=timeout_s)
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch entity overview via GetLiveContext: {e}")
            return None


def _extract_overview(raw: str) -> Optional[str]:
    """Turn GetLiveContext output into a names/domain/areas/device_class list."""
    text = raw
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            if payload.get("success") is False:
                logger.warning(f"⚠️ GetLiveContext failed: {payload.get('error')}")
                return None
            text = payload.get("result", "") or ""
    except (ValueError, TypeError):
        pass  # already plain text

    kept = []
    in_attributes = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith("- "):
            in_attributes = False
            kept.append(line)
        elif line.startswith("  ") and not line.startswith("    "):
            in_attributes = stripped.startswith("attributes:")
            if stripped.startswith(("domain:", "areas:")):
                kept.append(line)
        elif in_attributes and stripped.startswith("device_class:"):
            kept.append("  " + stripped)
        # anything else (header line, state, other attributes) is dropped
    return "\n".join(kept) if kept else None






