from mirage.progress import GenerationProgress

import pytest


def test_generation_progress_uses_sampling_steps_and_ordered_phases():
    progress = GenerationProgress()
    assert progress.update("  |##################################################| 265/265 - 3.49GB/s") == {}
    assert progress.update("[V] conditioning cache stored (1/4)") == {}
    assert progress.update("[I] computing condition graph completed, taking 1953 ms") == {"stage": "conditioning", "progress": 0.03}
    assert progress.update("[I] sampling started: method=euler, steps=40") == {"stage": "sampling", "progress": 0.05}

    for step, speed in ((1, "45.78s/it"), (9, "39.07s/it"), (10, "2.1it/s")):
        line = f"  |=====>  | {step}/40 - {speed} - ETA 20:31"
        assert progress.update(line) == {"stage": f"sampling:{step}/40", "progress": pytest.approx(0.05 + 0.9 * step / 40)}
        assert progress.update(line) == {}

    assert progress.update("  |=>       | 1/40 - 45.78s/it") == {}
    assert progress.update("[I] loading 140/251 tensors from /models/vae") == {}
    assert progress.update("  |==========| 40/40 - 39.15s/it") == {"stage": "sampling:40/40", "progress": 0.95}
    assert progress.update("[I] sampling completed, taking 1560.00s") == {"stage": "decoding", "progress": 0.95}
    assert progress.update("  |==========| 40/40 - 39.15s/it") == {}
    assert progress.update("[I] save result image 0 to '/output/test.png' (success)") == {"stage": "saving", "progress": 0.99}
