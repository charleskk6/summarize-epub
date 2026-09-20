from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5"
    vision_model: str = ""
    max_tokens: int = 8192
    temperature: float = 0.3
    timeout: float = 180.0
    concurrency: int = 4
    input_price: float = 0.0
    output_price: float = 0.0

    @classmethod
    def load(cls, cli: Mapping[str, Any], *, env: Mapping[str, str] | None = None,
             dotenv_path: Path | None = Path(".env"), require_key: bool = True) -> Config:
        if env is None:
            if dotenv_path is not None:
                load_dotenv(dotenv_path, override=False)
            env = os.environ
        defaults = cls()
        values: dict[str, Any] = {}
        for f in fields(cls):
            raw = cli.get(f.name)
            if raw is None:
                raw = env.get("LLM_" + f.name.upper(), getattr(defaults, f.name))
            try:
                values[f.name] = type(getattr(defaults, f.name))(raw)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid value for LLM_{f.name.upper()}") from None
        values["vision_model"] = values["vision_model"] or values["model"]
        cfg = cls(**values)
        if require_key and not cfg.api_key.strip():
            raise ValueError("LLM_API_KEY is missing. Set it in .env, the environment, or --api-key.")
        if cfg.max_tokens < 1 or cfg.concurrency < 1 or cfg.timeout <= 0:
            raise ValueError("max-tokens, concurrency and timeout must be positive")
        if not 0 <= cfg.temperature <= 2 or min(cfg.input_price, cfg.output_price) < 0:
            raise ValueError("temperature must be 0–2; prices must be non-negative")
        url = urlsplit(cfg.base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query:
            raise ValueError("base-url must be an HTTP(S) URL without credentials or a query")
        return cfg
