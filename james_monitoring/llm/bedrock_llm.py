"""Amazon Bedrock Converse via its scoped API key. No implicit AWS account/role fallback."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..secret_store import SecretStoreError
from . import LLMError, LLMResult
from .anthropic_llm import clean_messages


class BedrockLLM:
    name = "bedrock"

    def __init__(self, cfg):
        if not cfg.model:
            raise LLMError("Set a Bedrock model or inference-profile ID with Converse support.")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", cfg.region):
            raise LLMError("Invalid Bedrock AWS region.")
        if cfg.base_url:
            raise LLMError("Bedrock uses the regional AWS endpoint; custom base URLs are not supported.")
        try:
            self.key = cfg.api_key
        except SecretStoreError as e:
            raise LLMError(str(e)) from None
        if not self.key:
            raise LLMError("Bedrock API key missing. Save AWS_BEARER_TOKEN_BEDROCK in Secrets.")
        self.cfg = cfg

    def complete(self, system, messages):
        cfg = self.cfg
        suffix = "amazonaws.com.cn" if cfg.region.startswith("cn-") else "amazonaws.com"
        url = f"https://bedrock-runtime.{cfg.region}.{suffix}/model/{quote(cfg.model, safe='')}/converse"
        payload = {"system": [{"text": system}],
                   "messages": [{"role": m["role"], "content": [{"text": m["content"]}]}
                                for m in clean_messages(messages)],
                   "inferenceConfig": {"maxTokens": cfg.max_tokens, "temperature": cfg.temperature}}
        req = Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.key}, method="POST")
        try:
            with urlopen(req, timeout=120) as response:
                data = json.load(response)
        except HTTPError as e:
            # Do not expose provider bodies: they may echo prompts or secrets.
            raise LLMError(f"Bedrock call failed (HTTP {e.code}); check region and model access.") from None
        except (URLError, TimeoutError):
            raise LLMError("Bedrock connection failed or timed out.") from None
        except ValueError:
            raise LLMError("Bedrock returned invalid JSON.") from None
        try:
            content = data["output"]["message"]["content"]
            usage = data.get("usage", {})
            cached = int(usage.get("cacheReadInputTokens", 0))
            return LLMResult(text="".join(b.get("text", "") for b in content),
                input_tokens=int(usage.get("inputTokens", 0)) + cached + int(usage.get("cacheWriteInputTokens", 0)),
                output_tokens=int(usage.get("outputTokens", 0)), cached_tokens=cached,
                truncated=data.get("stopReason") == "max_tokens")
        except (KeyError, TypeError, ValueError):
            raise LLMError("Bedrock returned an invalid Converse response.") from None
