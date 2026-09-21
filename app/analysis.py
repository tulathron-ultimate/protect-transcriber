"""Ask an LLM about a transcript: summarise, review, and answer questions.

Deliberately speaks the OpenAI chat-completions shape and takes a configurable
base URL, so the same code works against OpenAI's own API or anything local that
mimics it -- Ollama, LM Studio, vLLM, LocalAI, OpenRouter.

Note for anyone wiring this up: a ChatGPT Plus/Pro subscription does **not**
include API access. OpenAI bills the API separately, so using api.openai.com here
needs a platform API key with its own credits. A local model costs nothing and
keeps the audio of your household off a third party's servers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .transcript import Segment

log = logging.getLogger(__name__)

# Roughly 4 characters per token, so this keeps a request near 12k tokens and
# leaves room for the reply on even a small local model.
DEFAULT_CHUNK_CHARS = 48_000


class AnalysisError(RuntimeError):
    """The analysis endpoint could not answer."""


class AnalysisNotConfigured(AnalysisError):
    """No endpoint has been set up yet."""


@dataclass(slots=True)
class AnalysisConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = False

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or "").rstrip("/")

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.model)

    @property
    def chat_url(self) -> str:
        # Accept a base with or without the /v1 suffix, which people get wrong
        # about half the time.
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if not re.search(r"/v\d+$", base):
            base = f"{base}/v1"
        return f"{base}/chat/completions"

    @property
    def models_url(self) -> str:
        base = self.base_url
        if not re.search(r"/v\d+$", base):
            base = f"{base}/v1"
        return f"{base}/models"


def format_transcript(segments: list[Segment], include_times: bool = True) -> str:
    """Timestamped lines, so the model can cite when something was said."""
    lines = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        if include_times:
            minutes, seconds = divmod(int(segment.start), 60)
            lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def chunk_transcript(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Split on line boundaries so a timestamped line is never cut in half."""
    if len(text) <= chunk_chars:
        return [text] if text else []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > chunk_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _extract_json(raw: str) -> Any:
    """Pull a JSON object out of a reply, tolerating code fences and preamble."""
    raw = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    # Fall back to the outermost {...} or [...] in the reply.
    match = re.search(r"[\[{].*[\]}]", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            pass
    raise AnalysisError("The model did not return usable JSON")


_GROUNDING = (
    "You are analysing a transcript produced by automatic speech recognition from a "
    "home security camera. It may contain mishearings, missing words and unattributed "
    "speakers. Use only what the transcript says. Never invent details, names or events. "
    "If something is unclear or not stated, say so plainly."
)


class AnalysisClient:
    def __init__(self, config: AnalysisConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=180.0))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def models(self) -> list[str]:
        response = await self._client.get(
            self.config.models_url, headers=self._headers(), timeout=15.0
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        return [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]

    async def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if not self.config.ready:
            raise AnalysisNotConfigured(
                "No analysis endpoint configured. Set one under Analysis in the UI."
            )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        if json_mode:
            # Honoured by OpenAI and most compatible servers; harmless elsewhere
            # because the prompt also asks for JSON and the parser is tolerant.
            payload["response_format"] = {"type": "json_object"}

        try:
            response = await self._client.post(
                self.config.chat_url, json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise AnalysisError(f"Cannot reach {self.config.base_url}: {exc}") from exc

        if response.status_code == 401:
            raise AnalysisError(
                "The analysis endpoint rejected the API key. Note that a ChatGPT "
                "subscription does not include API access -- api.openai.com needs a "
                "platform key with its own credits."
            )
        if response.status_code >= 400:
            raise AnalysisError(
                f"HTTP {response.status_code} from the model: {response.text[:300]}"
            )

        try:
            choices = response.json()["choices"]
            return (choices[0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise AnalysisError("The model returned an unexpected response shape") from exc

    # -- operations ---------------------------------------------------------

    async def summarize(self, segments: list[Segment]) -> dict[str, Any]:
        """A short summary plus bullet points, map-reduced over long transcripts."""
        text = format_transcript(segments)
        if not text.strip():
            return {"summary": "", "points": [], "note": "The transcript is empty."}

        chunks = chunk_transcript(text)
        if len(chunks) > 1:
            partials = []
            for index, chunk in enumerate(chunks, start=1):
                partials.append(
                    await self.chat(
                        _GROUNDING,
                        f"Part {index} of {len(chunks)} of a transcript. Summarise just this "
                        f"part in a few sentences.\n\n{chunk}",
                    )
                )
            text = "\n\n".join(partials)

        reply = await self.chat(
            _GROUNDING + ' Reply as JSON: {"summary": string, "points": [string], '
            '"speakers": string}. `summary` is two or three sentences. `points` is up to '
            "six short bullets of what actually happened. `speakers` describes how many "
            'distinct voices seem present, or "unclear".',
            f"Summarise this transcript.\n\n{text}",
            json_mode=True,
        )
        data = _extract_json(reply)
        return {
            "summary": str(data.get("summary", "")).strip(),
            "points": [str(p).strip() for p in (data.get("points") or []) if str(p).strip()][:8],
            "speakers": str(data.get("speakers", "")).strip(),
        }

    async def review(self, segments: list[Segment]) -> dict[str, Any]:
        """Flag content worth a human look, with a quote and timestamp for each."""
        text = format_transcript(segments)
        if not text.strip():
            return {"assessment": "The transcript is empty.", "flags": []}

        flags: list[dict[str, Any]] = []
        assessments: list[str] = []
        for chunk in chunk_transcript(text):
            reply = await self.chat(
                _GROUNDING
                + " You are flagging parts of a home security transcript that a resident "
                'might want to review. Reply as JSON: {"assessment": string, "flags": '
                '[{"category": string, "severity": "low"|"medium"|"high", '
                '"timestamp": string, "quote": string, "reason": string}]}. '
                "Categories to consider: threat, aggression, profanity, distress, "
                "solicitation, scam, medical emergency, trespass. Quote the transcript "
                "exactly and copy its [mm:ss] timestamp. Flag only what is genuinely there; "
                "an empty list is the right answer for ordinary conversation. ASR errors can "
                "make innocent speech look alarming, so say when a flag is uncertain.",
                f"Review this transcript.\n\n{chunk}",
                json_mode=True,
            )
            data = _extract_json(reply)
            assessments.append(str(data.get("assessment", "")).strip())
            for flag in data.get("flags") or []:
                if not isinstance(flag, dict):
                    continue
                severity = str(flag.get("severity", "low")).lower()
                flags.append(
                    {
                        "category": str(flag.get("category", "other")).strip()[:40],
                        "severity": severity if severity in ("low", "medium", "high") else "low",
                        "timestamp": str(flag.get("timestamp", "")).strip()[:12],
                        "quote": str(flag.get("quote", "")).strip()[:400],
                        "reason": str(flag.get("reason", "")).strip()[:400],
                    }
                )

        order = {"high": 0, "medium": 1, "low": 2}
        flags.sort(key=lambda f: order.get(f["severity"], 3))
        return {
            "assessment": " ".join(a for a in assessments if a).strip(),
            "flags": flags,
        }

    async def ask(self, segments: list[Segment], question: str) -> dict[str, Any]:
        """Answer a question using only the transcript."""
        text = format_transcript(segments)
        if not text.strip():
            return {"answer": "The transcript is empty, so there is nothing to answer from."}

        chunks = chunk_transcript(text)
        if len(chunks) > 1:
            # Answer per chunk, then reconcile, so a long clip still fits.
            partials = []
            for chunk in chunks:
                partials.append(
                    await self.chat(
                        _GROUNDING,
                        f"Using only this part of a transcript, answer: {question}\n"
                        f"If this part does not address it, reply exactly NOTHING RELEVANT.\n\n"
                        f"{chunk}",
                    )
                )
            useful = [p for p in partials if "NOTHING RELEVANT" not in p.upper()]
            if not useful:
                return {"answer": "The transcript does not say."}
            text = "\n\n".join(useful)

        answer = await self.chat(
            _GROUNDING + " Answer in a few sentences. Quote the transcript with its [mm:ss] "
            "timestamp when it supports your answer. If the transcript does not say, reply "
            "that it does not say.",
            f"Question: {question}\n\nTranscript:\n{text}",
        )
        return {"answer": answer}
