"""Voice commands from Home Assistant automations with sentence triggers.

With the stock Voice PE firmware, speech went through Home Assistant's own Assist
pipeline, so automations with a `conversation` (sentence) trigger fired when their
sentence was spoken. With this add-on, speech goes straight to OpenAI and Home
Assistant never sees the sentence, so those automations never fire.

This module restores that: it offers OpenAI a `run_voice_command` tool that

1. lists the sentences of all enabled automations with a sentence trigger, read
   from Home Assistant (refreshed when a new OpenAI session starts, at most every
   few minutes), so OpenAI can map the user's wording onto an exact trigger
   sentence;
2. forwards a sentence to Home Assistant's built-in conversation agent ONLY if it
   matches one of those triggers (strict check: the tool cannot be used to run
   arbitrary Assist commands), and
3. returns Home Assistant's reply (e.g. from `set_conversation_response`) for the
   assistant to speak, or a "no match" hint with the list so OpenAI can retry.

Exposure is not needed: automations do not have to be exposed to Assist for this.
Leaving them un-exposed means OpenAI cannot switch them on or off via MCP.
"""
import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

TOOL_NAME = "run_voice_command"
CATALOG_TTL_S = 300.0          # re-read automations at most every 5 minutes
MAX_SENTENCES_LISTED = 6       # per automation, in the tool description
HA_PROCESS_TIMEOUT_S = 30.0    # an automation may run for a while
_FETCH_CONCURRENCY = 8


def ha_api_base(ha_mcp_url: str) -> str:
    """Core REST API base (".../api") derived from the MCP endpoint URL."""
    base = (ha_mcp_url or "").rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base


# --- sentence template -> regex ---------------------------------------------
#
# Home Assistant sentence templates (hassil): [optional], (alt|ernatives),
# {slot}, <rule>. Matching is case-insensitive and ignores punctuation. This is
# a deliberately small converter used only as a GATE ("does this sentence belong
# to a voice-command automation?"); Home Assistant does the real matching.

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize(text: str) -> str:
    return " ".join(_WORD_RE.sub(" ", (text or "").lower()).split())


def _literal(chunk: str) -> str:
    words = normalize(chunk).split()
    return r"\s*".join(re.escape(w) for w in words)


def _parse(template: str, i: int = 0, closer: Optional[str] = None):
    """Return (regex, next_index) for template[i:] up to `closer`."""
    alternatives: List[str] = []
    current: List[str] = []
    buf = ""
    while i < len(template):
        ch = template[i]
        if closer and ch == closer:
            break
        if ch in "([":
            current.append(_literal(buf)); buf = ""
            inner, i = _parse(template, i + 1, ")" if ch == "(" else "]")
            current.append(f"(?:{inner})" + ("?" if ch == "[" else ""))
            i += 1  # skip the closer
            continue
        if ch in "{<":
            current.append(_literal(buf)); buf = ""
            end = template.find("}" if ch == "{" else ">", i)
            i = len(template) if end < 0 else end + 1
            current.append(r".+?")
            continue
        if ch == "|":
            current.append(_literal(buf)); buf = ""
            alternatives.append(r"\s*".join(p for p in current if p))
            current = []
            i += 1
            continue
        buf += ch
        i += 1
    current.append(_literal(buf))
    alternatives.append(r"\s*".join(p for p in current if p))
    return "|".join(alternatives), i


def template_to_regex(template: str) -> Optional[re.Pattern]:
    try:
        body, _ = _parse(template)
        return re.compile(rf"^\s*(?:{body})\s*$", re.UNICODE)
    except re.error as e:
        logger.debug(f"🗣️ could not convert sentence template {template!r}: {e!r}")
        return None


# --- catalog ----------------------------------------------------------------

def _sentences_from_config(cfg: Dict[str, Any]) -> List[str]:
    triggers = cfg.get("triggers", cfg.get("trigger")) or []
    if isinstance(triggers, dict):
        triggers = [triggers]
    out: List[str] = []
    for t in triggers:
        if not isinstance(t, dict):
            continue
        kind = t.get("trigger", t.get("platform"))
        if kind != "conversation":
            continue
        cmd = t.get("command") or []
        if isinstance(cmd, str):
            cmd = [cmd]
        out.extend(str(c).strip() for c in cmd if str(c).strip())
    return out


class SentenceCommandCatalog:
    """Voice-command automations read from Home Assistant, cached."""

    def __init__(self, ha_mcp_url: str, access_token: Optional[str]):
        self._base = ha_api_base(ha_mcp_url)
        self._token = access_token
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self.language: str = ""
        # [{"name": str, "entity_id": str, "sentences": [str], "patterns": [re]}]
        self.commands: List[Dict[str, Any]] = []

    @property
    def available(self) -> bool:
        return bool(self._base and self._token)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def refresh(self, force: bool = False) -> None:
        if not self.available:
            return
        async with self._lock:
            if not force and self.commands and time.monotonic() - self._loaded_at < CATALOG_TTL_S:
                return
            try:
                await self._load()
                self._loaded_at = time.monotonic()
            except Exception as e:
                logger.warning(f"⚠️ Could not read voice-command automations from Home Assistant: {e!r}")

    async def _load(self) -> None:
        async with httpx.AsyncClient(timeout=10.0, headers=self._headers()) as client:
            if not self.language:
                try:
                    cfg = (await client.get(f"{self._base}/config")).json()
                    self.language = str(cfg.get("language") or "")
                except Exception:
                    pass
            states = (await client.get(f"{self._base}/states")).json()
            autos = [
                s for s in states
                if str(s.get("entity_id", "")).startswith("automation.")
                and s.get("state") == "on"          # disabled ones don't fire anyway
                and (s.get("attributes") or {}).get("id")
            ]
            sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

            async def fetch(s):
                async with sem:
                    auto_id = s["attributes"]["id"]
                    try:
                        r = await client.get(f"{self._base}/config/automation/config/{auto_id}")
                        if r.status_code != 200:
                            return None
                        sentences = _sentences_from_config(r.json())
                    except Exception:
                        return None
                    if not sentences:
                        return None
                    patterns = [p for p in (template_to_regex(x) for x in sentences) if p]
                    return {
                        "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"],
                        "entity_id": s["entity_id"],
                        "sentences": sentences,
                        "patterns": patterns,
                    }

            found = [c for c in await asyncio.gather(*(fetch(s) for s in autos)) if c]
        self.commands = found
        logger.info(
            f"🗣️ Voice commands: {len(found)} automation(s) with sentence triggers "
            f"({sum(len(c['sentences']) for c in found)} sentences, language={self.language or '?'})"
        )

    def match(self, sentence: str) -> Optional[Dict[str, Any]]:
        text = normalize(sentence)
        for c in self.commands:
            if any(p.match(text) for p in c["patterns"]):
                return c
        return None

    def listing(self, per_command: int = MAX_SENTENCES_LISTED) -> str:
        lines = []
        for c in self.commands:
            shown = c["sentences"][:per_command]
            more = len(c["sentences"]) - len(shown)
            line = " · ".join(f'"{s}"' for s in shown)
            if more > 0:
                line += f" (+{more} more variants)"
            lines.append(f"- {line}")
        return "\n".join(lines)

    async def process(self, sentence: str) -> Dict[str, Any]:
        payload = {"text": sentence, "agent_id": "conversation.home_assistant"}
        if self.language:
            payload["language"] = self.language
        async with httpx.AsyncClient(timeout=HA_PROCESS_TIMEOUT_S, headers=self._headers()) as client:
            r = await client.post(f"{self._base}/conversation/process", json=payload)
            r.raise_for_status()
            return r.json()


def get_voice_command_tool_definition(catalog: SentenceCommandCatalog) -> Dict[str, Any]:
    return {
        "type": "function",
        "name": TOOL_NAME,
        "description": (
            "Run one of the user's own Home Assistant voice commands (automations "
            "with sentence triggers). Use this whenever the request matches one of "
            "the commands below, even if worded differently. Pass the matching "
            "command sentence from the list VERBATIM, in its original language "
            "(fill any {placeholder} with what the user said). Speak the result "
            "the tool returns. Available commands (each line = one automation, "
            "alternatives separated by ·):\n" + catalog.listing()
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sentence": {
                    "type": "string",
                    "description": "A command sentence from the list, verbatim.",
                }
            },
            "required": ["sentence"],
        },
    }


def create_voice_command_handler(
    catalog: SentenceCommandCatalog,
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    async def handler(params: "FunctionCallParams") -> None:
        sentence = str((params.arguments or {}).get("sentence", "")).strip()
        logger.info(f"🗣️ run_voice_command: {sentence!r}")
        cmd = catalog.match(sentence)
        if cmd is None:
            await params.result_callback(
                "No voice command matches that sentence. Use one of these exactly "
                "(or another tool if none fits):\n" + catalog.listing(per_command=3)
            )
            return
        try:
            result = await catalog.process(sentence)
        except httpx.TimeoutException:
            await params.result_callback(
                f"The command ({cmd['name']}) was started and is still running."
            )
            return
        except Exception as e:
            logger.error(f"❌ run_voice_command failed: {e!r}")
            await params.result_callback("Home Assistant could not run the command just now.")
            return
        response = (result or {}).get("response") or {}
        speech = (((response.get("speech") or {}).get("plain") or {}).get("speech") or "").strip()
        if response.get("response_type") == "error":
            code = (response.get("data") or {}).get("code", "")
            logger.info(f"🗣️ Home Assistant did not run it ({code}): {speech}")
            await params.result_callback(
                f"Home Assistant did not run the command ({code or 'error'}). {speech}".strip()
            )
            return
        logger.info(f"🗣️ ran {cmd['entity_id']}: {speech[:120]}")
        await params.result_callback(speech or "Done.")

    return handler
