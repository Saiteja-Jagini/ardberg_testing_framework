import asyncio
import json
from typing import TypeVar
from pydantic import BaseModel
from openai import OpenAI
from .config import settings

T = TypeVar("T", bound=BaseModel)


def _parse(system_prompt: str, payload: dict, schema: type[T]) -> T:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for agent generation")
    client = OpenAI(api_key=settings.openai_api_key, timeout=settings.node_timeout_seconds)
    response = client.responses.parse(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text_format=schema,
    )
    if response.output_parsed is None:
        raise RuntimeError("Model did not return a parsed response")
    return response.output_parsed


async def parse(system_prompt: str, payload: dict, schema: type[T]) -> T:
    return await asyncio.to_thread(_parse, system_prompt, payload, schema)
