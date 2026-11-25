from datetime import datetime
import os
from pathlib import Path

from .errors import WorkbenchError


def reserve_output(directory: Path, model_label, resolution, quality, name_exists=None):
    prefix = f"{datetime.now().strftime('%Y%m%d')}_{model_label}_{resolution}_{quality}"
    for serial in range(1, 1000000):
        final = directory / f"{prefix}-{serial:04d}.png"
        if name_exists and name_exists(final.name):
            continue
        try:
            descriptor = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        else:
            os.close(descriptor)
            return final
    raise WorkbenchError("output_serial_exhausted", "No output serial number is available", 500)
