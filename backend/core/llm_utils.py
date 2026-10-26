"""LLM response helpers shared by Anthropic-compatible providers."""
import json
import os
from typing import Any, Iterable, List


def extract_text_content(content: Iterable[Any]) -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return "\n".join(t for t in texts if t)


class StructuredResponseError(ValueError):
    """A stable parsing failure that never contains response text or thinking."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__('structured_response_' + reason)


def structured_thinking_policy(value=None) -> str:
    """Read the explicit vendor compatibility policy; defaults send no extension."""
    policy = os.getenv('ECHOMIND_STRUCTURED_THINKING', 'provider_default') if value is None else value
    if policy not in {'provider_default', 'disabled'}:
        raise ValueError('Invalid structured thinking policy')
    return policy


def structured_request_options() -> dict:
    """Only structured calls opt into a vendor's thinking-disabled extension.

    Anthropic SDK 0.40.0 supports extra_body, not a typed thinking parameter.
    Native providers receive no extension unless the caller explicitly opts in.
    """
    if structured_thinking_policy() == 'disabled':
        return {'extra_body': {'thinking': {'type': 'disabled'}}}
    return {}


def parse_structured_response(response: Any, expected_type: type) -> Any:
    """Decode the first JSON object/array from text blocks, including code fences.

    Never parse thinking or recover a partial response after token exhaustion.
    Domain validation (scores, permutations, profile schema) stays with callers.
    """
    if expected_type not in (dict, list):
        raise ValueError('Structured response type must be dict or list')
    stop_reason = response.get('stop_reason') if isinstance(response, dict) else getattr(response, 'stop_reason', None)
    if stop_reason == 'max_tokens':
        raise StructuredResponseError('max_tokens')
    content = response.get('content') if isinstance(response, dict) else getattr(response, 'content', None)
    raw = extract_text_content(content).strip()
    if not raw:
        raise StructuredResponseError('empty_text')
    # Decode at the first top-level container, so an array cannot accidentally
    # become a dict merely because it contains an inner object.
    starts = [index for index in (raw.find('{'), raw.find('[')) if index >= 0]
    if not starts:
        raise StructuredResponseError('invalid_json')
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[min(starts):])
    except (ValueError, TypeError):
        raise StructuredResponseError('invalid_json') from None
    if type(data) is not expected_type:
        raise StructuredResponseError('wrong_type')
    return data
