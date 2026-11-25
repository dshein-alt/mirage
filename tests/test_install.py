from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_shell_syntax():
    for path in [ROOT / "install.sh", *ROOT.glob("scripts/*.sh"), ROOT / "docker/export-runtime.sh"]:
        subprocess.run(["sh", "-n", str(path)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "bin/sdwb")], check=True)


def test_pinned_source_revision():
    source = ROOT / "stable-diffusion.cpp"
    if (source / ".git").exists():
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, text=True, capture_output=True)
        revision = result.stdout.strip()
    else:
        import json
        revision = json.loads((ROOT / "config/models.json").read_text())["minimum_sd_revision"]
    assert revision == "532d2c15c430a785a712b2d94281f04e733008a4"
