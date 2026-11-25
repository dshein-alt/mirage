import re

FRACTION = re.compile(r"(?:^|\s)(\d{1,4})\s*/\s*(\d{1,4})(?:\s|$)")
PERCENT = re.compile(r"(?:^|\s)(\d{1,3})%(?:\s|$)")


def parse_progress(line):
    match = FRACTION.search(line)
    if match:
        current, total = int(match.group(1)), int(match.group(2))
        if total > 0 and current <= total:
            return min(0.99, current / total)
    match = PERCENT.search(line)
    if match:
        return min(0.99, int(match.group(1)) / 100)
    return None


def parse_stage(line):
    lowered = line.lower()
    for stage, words in (
        ("conditioning", ("condition", "encoding", "llm")),
        ("decoding", ("vae decode", "decoding")),
        ("saving", ("saving", "save image")),
        ("sampling", ("sampling", "denoising", "step")),
        ("loading", ("loading", "load model")),
    ):
        if any(word in lowered for word in words):
            return stage
    return None
