import re

SAMPLING_STEP = re.compile(r"^\s*\|[=> ]+\|\s*(\d{1,4})/(\d{1,4})\s+-\s+[\d.]+(?:s/it|it/s)\b")
LOG_PREFIX = re.compile(r"^\[[DVIWE]\]\s+")
PHASE_PROGRESS = {"loading": 0.0, "conditioning": 0.03, "sampling": 0.05, "decoding": 0.95, "saving": 0.99}


class GenerationProgress:
    def __init__(self):
        self.stage = "loading"
        self.progress = 0.0

    def update(self, line):
        match = SAMPLING_STEP.match(line)
        if match:
            step, total = map(int, match.groups())
            if not 0 < step <= total:
                return {}
            stage = f"sampling:{step}/{total}"
            progress = 0.95 if step == total else 0.05 + 0.9 * step / total
        else:
            message = LOG_PREFIX.sub("", line).lower()
            if message.startswith("computing condition graph"):
                stage = "conditioning"
            elif message.startswith("sampling started:"):
                stage = "sampling"
            elif message.startswith(("sampling completed", "computing vae decode", "vae decode")):
                stage = "decoding"
            elif message.startswith(("save result image", "saving image")):
                stage = "saving"
            else:
                return {}
            progress = PHASE_PROGRESS[stage]

        phase = stage.split(":", 1)[0]
        previous_phase = self.stage.split(":", 1)[0]
        if PHASE_PROGRESS[phase] < PHASE_PROGRESS[previous_phase] or progress < self.progress:
            return {}
        if (stage, progress) == (self.stage, self.progress):
            return {}
        self.stage, self.progress = stage, progress
        return {"stage": stage, "progress": progress}
