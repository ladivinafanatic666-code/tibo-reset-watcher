"""Export a source-only folder / ZIP. Never copy local settings or credentials."""
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = (
    ".gitignore", ".github/workflows/reset-watch.yml", ".github/workflows/tests.yml",
    "README.md", "docs/EVENTS.md", "watcher.py", "community.py", "event_protocol.py",
    "cloud_watch.py", "app.py", "start.cmd", "config.example.json",
    "test_watcher.py", "test_community.py", "prepare_upload.py",
    "codex_bridge.py", "test_codex_bridge.py", "codex-bridge.example.json",
    ".github/workflows/codex-signal-test.yml", "docs/AUTOMATION.md",
)


def main():
    target = ROOT / "github-upload"
    if target.exists():
        known = {Path(name).as_posix() for name in FILES}
        unexpected = [p for p in target.rglob("*") if p.is_file() and p.relative_to(target).as_posix() not in known]
        if unexpected:
            raise RuntimeError("Upload folder contains unexpected files; use a fresh source folder.")
    for name in FILES:
        source = ROOT / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"Missing or unexpected source file: {name}")
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    archive = ROOT / "tibo-reset-watcher.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name in FILES:
            output.write(ROOT / name, name)
    print(f"Source folder: {target}")
    print(f"Source archive: {archive}")
    print(f"Included {len(FILES)} source files; excluded config.json, data and credentials.")


if __name__ == "__main__":
    main()
