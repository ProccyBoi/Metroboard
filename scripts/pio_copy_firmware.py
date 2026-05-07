from pathlib import Path
import json
import re
import shutil

Import("env")


def _sketch_version(project_dir: Path) -> str:
    sketch = project_dir / "metroboard_public.ino"
    body = sketch.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'kFirmwareVersion\s*=\s*"([^"]+)"', body)
    if not match:
        raise RuntimeError(f"Could not find kFirmwareVersion in {sketch}")
    return match.group(1).strip()


def _validate_manifest(project_dir: Path, version: str) -> None:
    manifest_path = project_dir / "firmware" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_version = str(data.get("version") or "").strip()
    filename = str(data.get("filename") or "").strip()
    if manifest_version != version:
        raise RuntimeError(
            f"Firmware version mismatch: sketch has {version}, "
            f"manifest has {manifest_version}"
        )
    if filename != "metroboard_public.bin":
        raise RuntimeError(
            f"Unexpected firmware manifest filename: {filename!r}; "
            "expected 'metroboard_public.bin'"
        )


def copy_firmware(target, source, env):
    project_dir = Path(env.subst("$PROJECT_DIR"))
    version = _sketch_version(project_dir)
    _validate_manifest(project_dir, version)
    source_bin = Path(str(target[0]))
    firmware_dir = project_dir / "firmware"
    firmware_dir.mkdir(exist_ok=True)
    dest = firmware_dir / "metroboard_public.bin"
    shutil.copy2(source_bin, dest)
    print(f"[metroboard] OTA firmware {version} copied to {dest}")


env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", copy_firmware)
