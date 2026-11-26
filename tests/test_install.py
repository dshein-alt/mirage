import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_shell_syntax():
    for path in [ROOT / "install.sh", *ROOT.glob("scripts/*.sh"), ROOT / "docker/export-runtime.sh"]:
        subprocess.run(["sh", "-n", str(path)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "bin/mirage")], check=True)


def test_pinned_source_revision():
    registry = json.loads((ROOT / "config/models.json").read_text())
    settings = json.loads((ROOT / "config/settings.default.json").read_text())
    revision = registry["minimum_sd_revision"]
    assert settings["runtime"]["minimum_sd_revision"] == revision
    assert f"ARG SD_REVISION={revision}" in (ROOT / "docker/Dockerfile.builder").read_text()

    source = ROOT / "stable-diffusion.cpp"
    if (source / ".git").exists():
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, text=True, capture_output=True)
        assert result.stdout.strip() == revision
