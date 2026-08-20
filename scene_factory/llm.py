from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .intent import SceneIntent, scene_intent_schema
from .paths import project_root


class LLMParserError(RuntimeError):
    pass


class IntentParser(Protocol):
    @property
    def name(self) -> str: ...

    def parse(self, prompt: str) -> SceneIntent: ...

    def revise(self, current: SceneIntent, instruction: str) -> SceneIntent: ...


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 60.0
    cache_dir: Path = Path(".cache/llm_intents")
    ca_bundle: str | Path | None = None
    transport: str = "urllib"
    proxy_url: str = ""

    @property
    def endpoint(self) -> str:
        value = self.base_url.rstrip("/")
        return value if value.endswith("/chat/completions") else f"{value}/chat/completions"


class StructuredLLMIntentParser:
    """Parse natural language with a JSON-schema capable chat-completions endpoint."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        categories: list[str],
        room_types: list[str],
        events: list[str],
    ) -> None:
        if not config.base_url or not config.model:
            raise ValueError("LLM base_url and model are required")
        self.config = config
        self.categories = sorted(set(categories))
        self.room_types = sorted(set(room_types))
        self.events = sorted(set(events))
        self.schema = scene_intent_schema(self.categories, self.room_types, self.events)

    @property
    def name(self) -> str:
        return f"llm:{self.config.model}"

    def parse(self, prompt: str) -> SceneIntent:
        normalized = prompt.strip()
        if len(normalized) < 2:
            raise ValueError("prompt must contain at least two characters")
        cached = self._read_cache(normalized)
        if cached is not None:
            return self._intent_from_payload(cached)

        raw = self._request(normalized)
        intent = self._intent_from_payload(raw)
        self._write_cache(normalized, intent.to_dict())
        return intent

    def revise(self, current: SceneIntent, instruction: str) -> SceneIntent:
        """Apply a natural-language edit while preserving the rest of a SceneIntent."""
        normalized = instruction.strip()
        if len(normalized) < 2:
            raise ValueError("revision instruction must contain at least two characters")
        if len(normalized) > 1000:
            raise ValueError("revision instruction must not exceed 1000 characters")

        current_json = json.dumps(
            current.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        cache_identity = f"scene-intent-revision-v1\n{current_json}\n{normalized}"
        cached = self._read_cache(cache_identity)
        if cached is not None:
            return self._intent_from_payload(cached)

        user_prompt = (
            "Current SceneIntent JSON:\n"
            f"{current_json}\n\n"
            "Revision instruction:\n"
            f"{normalized}"
        )
        revision_rules = (
            " This is an incremental revision. Return the complete revised SceneIntent, not a "
            "patch. Preserve every object, relation, object_id, room_type, event, room dimensions, "
            "and scene property that the instruction does not explicitly change. Remove dangling "
            "relations when deleting an object. When adding instances, use new unique snake_case "
            "IDs. Reflect the final scene in description."
        )
        raw = self._request(user_prompt, system_suffix=revision_rules)
        intent = self._intent_from_payload(raw)
        self._write_cache(cache_identity, intent.to_dict())
        return intent

    def test_connection(self) -> dict[str, Any]:
        """Make one uncached structured request and validate the returned SceneIntent."""
        started = time.perf_counter()
        payload = self._request("连接测试：厨房台面上放着一个杯子。")
        intent = self._intent_from_payload(payload)
        return {
            "ok": True,
            "parser": self.name,
            "endpoint": self.config.endpoint,
            "model": self.config.model,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "sample": {
                "room_type": intent.room_type,
                "event": intent.event,
                "object_count": len(intent.objects),
            },
        }

    def _intent_from_payload(self, payload: dict[str, Any]) -> SceneIntent:
        return SceneIntent.from_dict(
            payload,
            allowed_categories=self.categories,
            allowed_room_types=self.room_types,
            allowed_events=self.events,
        )

    def _request(self, prompt: str, *, system_suffix: str = "") -> dict[str, Any]:
        system_prompt = (
            "You compile household scene descriptions into SceneIntent JSON. "
            "Use only category, room_type, event and predicate values allowed by the schema. "
            "Create one object entry per distinct instance and use stable snake_case object IDs. "
            "Relations describe semantics only; never output coordinates, asset IDs, USD paths, "
            "robot actions, or commentary. Include every relation endpoint in objects. "
            "Mark furniture dynamic=false and movable clutter dynamic=true. "
            "If a described object is unavailable, omit it instead of inventing a category."
            + system_suffix
            + " The complete required JSON Schema follows:\n"
            + json.dumps(self.schema, ensure_ascii=False, sort_keys=True)
        )
        base_payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        strict_payload = {
            **base_payload,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "scene_intent",
                    "strict": True,
                    "schema": self.schema,
                },
            },
        }
        try:
            response = self._post(strict_payload)
        except LLMParserError as exc:
            if "HTTP 400" not in str(exc) and "HTTP 422" not in str(exc):
                raise
            response = self._post(
                {**base_payload, "response_format": {"type": "json_object"}}
            )
        return self._extract_json(response)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.config.transport == "curl_schannel":
            return self._post_curl_schannel(payload)
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = Request(
            self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            context = self._ssl_context()
            with urlopen(
                request,
                timeout=self.config.timeout_seconds,
                context=context,
            ) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise LLMParserError(f"LLM HTTP {exc.code}: {details[:500]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise LLMParserError(f"LLM request failed: {exc}") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMParserError("LLM endpoint returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise LLMParserError("LLM endpoint returned a non-object response")
        return decoded

    def _post_curl_schannel(self, payload: dict[str, Any]) -> dict[str, Any]:
        if os.name != "nt":
            raise LLMParserError("curl_schannel transport is available only on Windows")
        header_path: Path | None = None
        try:
            arguments = [
                "curl.exe",
                "--ssl-no-revoke",
                "--tlsv1.2",
                "--tls-max",
                "1.2",
                "--http1.1",
                "--silent",
                "--show-error",
                "--request",
                "POST",
                self.config.endpoint,
                "--header",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
                "--connect-timeout",
                str(max(1, min(20, int(self.config.timeout_seconds)))),
                "--max-time",
                str(max(1, int(self.config.timeout_seconds))),
                "--write-out",
                "\n%{http_code}",
            ]
            if self.config.proxy_url:
                arguments.extend(["--proxy", self.config.proxy_url])
            if self.config.api_key:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".headers",
                    prefix="scene-factory-llm-",
                    delete=False,
                ) as temporary:
                    temporary.write(f"Authorization: Bearer {self.config.api_key}\r\n")
                    header_path = Path(temporary.name)
                arguments.extend(["--header", f"@{header_path}"])

            serialized_payload = json.dumps(payload, ensure_ascii=False)
            completed = None
            for attempt in range(5):
                completed = subprocess.run(
                    arguments,
                    input=serialized_payload,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.config.timeout_seconds + 5,
                    check=False,
                )
                if completed.returncode not in {35, 56} or attempt == 4:
                    break
                time.sleep(min(8.0, float(2**attempt)))
        except FileNotFoundError as exc:
            raise LLMParserError("curl_schannel transport requires curl.exe") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMParserError("LLM curl request timed out") from exc
        finally:
            if header_path is not None:
                header_path.unlink(missing_ok=True)

        if completed is None:
            raise LLMParserError("LLM curl transport did not start")
        if completed.returncode != 0:
            details = completed.stderr.strip()[:500]
            raise LLMParserError(
                f"LLM curl transport failed with exit {completed.returncode}: {details}"
            )
        body, separator, status_text = completed.stdout.rpartition("\n")
        if not separator or not status_text.strip().isdigit():
            raise LLMParserError("LLM curl transport returned no HTTP status")
        status = int(status_text.strip())
        if status >= 400:
            raise LLMParserError(f"LLM HTTP {status}: {body[:500]}")
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMParserError("LLM endpoint returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise LLMParserError("LLM endpoint returned a non-object response")
        return decoded

    def _ssl_context(self) -> ssl.SSLContext | None:
        bundle = self.config.ca_bundle
        if bundle is None or str(bundle).strip().lower() in {"", "system"}:
            return None
        if str(bundle).strip().lower() == "certifi":
            try:
                import certifi
            except ImportError as exc:
                raise LLMParserError(
                    "ca_bundle=certifi requires the certifi Python package"
                ) from exc
            cafile = certifi.where()
        else:
            cafile = str(Path(bundle).expanduser().resolve())
            if not Path(cafile).is_file():
                raise LLMParserError(f"LLM CA bundle does not exist: {cafile}")
        try:
            return ssl.create_default_context(cafile=cafile)
        except (OSError, ssl.SSLError) as exc:
            raise LLMParserError(f"could not load LLM CA bundle: {exc}") from exc

    @staticmethod
    def _extract_json(response: dict[str, Any]) -> dict[str, Any]:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMParserError("LLM response has no choices[0].message.content") from exc
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        if not isinstance(content, str):
            raise LLMParserError("LLM response content is not text")
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise LLMParserError("LLM content is not valid SceneIntent JSON") from exc
        if not isinstance(payload, dict):
            raise LLMParserError("LLM SceneIntent must be a JSON object")
        return payload

    def _cache_path(self, prompt: str) -> Path:
        identity = json.dumps(
            {
                "version": 1,
                "endpoint": self.config.endpoint,
                "model": self.config.model,
                "categories": self.categories,
                "room_types": self.room_types,
                "events": self.events,
                "prompt": prompt,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.config.cache_dir / f"{digest}.json"

    def _read_cache(self, prompt: str) -> dict[str, Any] | None:
        path = self._cache_path(prompt)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_cache(self, prompt: str, payload: dict[str, Any]) -> None:
        path = self._cache_path(prompt)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def load_llm_settings() -> dict[str, Any]:
    """Load non-secret settings from config/llm.json, then apply environment overrides."""
    configured_path = os.environ.get("SCENE_FACTORY_LLM_CONFIG", "").strip()
    config_path = (
        Path(configured_path).expanduser().resolve()
        if configured_path
        else project_root() / "config" / "llm.json"
    )
    raw: dict[str, Any] = {}
    if config_path.is_file():
        try:
            decoded = json.loads(config_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid LLM config file {config_path}: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ValueError(f"LLM config must be a JSON object: {config_path}")
        raw = decoded
    if "api_key" in raw:
        raise ValueError(
            "do not store api_key in config/llm.json; use SCENE_FACTORY_LLM_API_KEY"
        )

    def value(env_name: str, key: str, default: Any) -> Any:
        return os.environ[env_name] if env_name in os.environ else raw.get(key, default)

    mode = str(value("SCENE_FACTORY_LLM_MODE", "mode", "auto")).strip().lower()
    if mode not in {"auto", "off", "required"}:
        raise ValueError("LLM mode must be auto, off, or required")
    base_url = str(value("SCENE_FACTORY_LLM_BASE_URL", "base_url", "")).strip()
    model = str(value("SCENE_FACTORY_LLM_MODEL", "model", "")).strip()
    if bool(base_url) != bool(model):
        raise ValueError("LLM base_url and model must be configured together")
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LLM base_url must be an http:// or https:// URL")

    timeout = float(
        value("SCENE_FACTORY_LLM_TIMEOUT_SECONDS", "timeout_seconds", 60)
    )
    if not 0 < timeout <= 300:
        raise ValueError("LLM timeout_seconds must be greater than 0 and at most 300")

    configured_cache = str(value("SCENE_FACTORY_LLM_CACHE", "cache_dir", "")).strip()
    if configured_cache:
        cache_path = Path(configured_cache).expanduser()
        if not cache_path.is_absolute():
            cache_path = config_path.parent / cache_path
        cache_dir = cache_path.resolve()
    else:
        cache_dir = project_root() / ".cache" / "llm_intents"

    configured_ca = str(
        value("SCENE_FACTORY_LLM_CA_BUNDLE", "ca_bundle", "system")
    ).strip()
    if configured_ca.lower() in {"", "system", "certifi"}:
        ca_bundle: str | Path | None = configured_ca.lower() or "system"
    else:
        ca_path = Path(configured_ca).expanduser()
        if not ca_path.is_absolute():
            ca_path = config_path.parent / ca_path
        ca_bundle = ca_path.resolve()
        if not ca_bundle.is_file():
            raise ValueError(f"LLM ca_bundle does not exist: {ca_bundle}")

    transport = str(
        value("SCENE_FACTORY_LLM_TRANSPORT", "transport", "urllib")
    ).strip().lower()
    if transport not in {"urllib", "curl_schannel"}:
        raise ValueError("LLM transport must be urllib or curl_schannel")

    proxy_url = str(
        value("SCENE_FACTORY_LLM_PROXY_URL", "proxy_url", "")
    ).strip()
    if proxy_url:
        parsed_proxy = urlsplit(proxy_url)
        if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.netloc:
            raise ValueError("LLM proxy_url must be an http:// or https:// URL")
        if parsed_proxy.username or parsed_proxy.password:
            raise ValueError("LLM proxy_url must not contain credentials")

    api_key_env = str(raw.get("api_key_env", "SCENE_FACTORY_LLM_API_KEY")).strip()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{2,63}", api_key_env) or "_" not in api_key_env:
        raise ValueError(
            "api_key_env must be an uppercase environment variable name such as "
            "SCENE_FACTORY_LLM_API_KEY, not an API key value"
        )
    api_key = os.environ.get("SCENE_FACTORY_LLM_API_KEY", "")
    if not api_key and api_key_env != "SCENE_FACTORY_LLM_API_KEY":
        api_key = os.environ.get(api_key_env, "")

    return {
        "mode": mode,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "timeout_seconds": timeout,
        "cache_dir": cache_dir,
        "ca_bundle": ca_bundle,
        "transport": transport,
        "proxy_url": proxy_url,
        "config_path": config_path.resolve(),
        "config_file_exists": config_path.is_file(),
    }


def create_intent_parser_from_env(
    *,
    categories: list[str],
    room_types: list[str],
    events: list[str],
    settings: dict[str, Any] | None = None,
) -> StructuredLLMIntentParser | None:
    settings = settings or load_llm_settings()
    mode = settings["mode"]
    if mode == "off":
        return None

    base_url = settings["base_url"]
    model = settings["model"]
    if not base_url or not model:
        if mode == "required":
            raise ValueError(
                "LLM mode is required but SCENE_FACTORY_LLM_BASE_URL or "
                "SCENE_FACTORY_LLM_MODEL is missing"
            )
        return None

    return StructuredLLMIntentParser(
        LLMConfig(
            base_url=base_url,
            model=model,
            api_key=settings["api_key"],
            timeout_seconds=settings["timeout_seconds"],
            cache_dir=settings["cache_dir"],
            ca_bundle=settings["ca_bundle"],
            transport=settings["transport"],
            proxy_url=settings["proxy_url"],
        ),
        categories=categories,
        room_types=room_types,
        events=events,
    )
