from sd_workbench.progress import parse_progress, parse_stage


def test_progress_is_opportunistic():
    assert parse_progress("step 3/8") == 3 / 8
    assert parse_progress(" 45% ") == 0.45
    assert parse_progress("unrelated") is None
    assert parse_stage("VAE decode complete") == "decoding"
    assert parse_stage("sampling step") == "sampling"
