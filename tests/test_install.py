from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_shell_syntax():
    for path in [ROOT / "install.sh", *ROOT.glob("scripts/*.sh"), ROOT / "docker/export-runtime.sh"]:
        subprocess.run(["sh", "-n", str(path)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "bin/mirage")], check=True)


def test_pinned_source_revision():
    source = ROOT / "stable-diffusion.cpp"
    if (source / ".git").exists():
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, text=True, capture_output=True)
        revision = result.stdout.strip()
    else:
        import json
        revision = json.loads((ROOT / "config/models.json").read_text())["minimum_sd_revision"]
    assert revision == "79dc7ca9074d79419a46380cdb95a4524db9b74c"
