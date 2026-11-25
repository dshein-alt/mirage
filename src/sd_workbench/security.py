import re
from pathlib import Path

from .errors import WorkbenchError

ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


async def bounded_upload(request, max_bytes):
    length = request.headers.get("Content-Length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError as error:
            raise WorkbenchError("upload_length_invalid", "Invalid Content-Length") from error
        if declared < 0:
            raise WorkbenchError("upload_length_invalid", "Invalid Content-Length")
        if declared > max_bytes:
            raise WorkbenchError("upload_too_large", f"Reference image exceeds {max_bytes} bytes", 413)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > max_bytes:
            raise WorkbenchError("upload_too_large", f"Reference image exceeds {max_bytes} bytes", 413)
        content.extend(chunk)
    return bytes(content)


def validate_prompt(prompt, max_length=16000):
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkbenchError("prompt_empty", "Prompt cannot be empty")
    if len(prompt) > max_length:
        raise WorkbenchError("prompt_too_long", f"Prompt exceeds {max_length} characters", 413)
    if "\x00" in prompt:
        raise WorkbenchError("prompt_invalid", "Prompt contains a null byte")
    return prompt


def validate_references(model, reference_ids, registry):
    if not isinstance(reference_ids, list) or len(reference_ids) > 8:
        raise WorkbenchError("references_invalid", "reference_ids must contain at most 8 image IDs")
    if any(not isinstance(value, str) or not ID_PATTERN.fullmatch(value) for value in reference_ids):
        raise WorkbenchError("references_invalid", "Invalid reference image ID")
    if reference_ids and "edit" not in registry.get(model)["tasks"]:
        raise WorkbenchError("edit_unsupported", f"Model {model} does not support reference-image editing")
    return reference_ids
