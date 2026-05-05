from pathlib import Path
import shutil

Import("env")


def copy_firmware(target, source, env):
    project_dir = Path(env.subst("$PROJECT_DIR"))
    source_bin = Path(str(target[0]))
    firmware_dir = project_dir / "firmware"
    firmware_dir.mkdir(exist_ok=True)
    dest = firmware_dir / "metroboard_public.bin"
    shutil.copy2(source_bin, dest)
    print(f"[metroboard] OTA firmware copied to {dest}")


env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", copy_firmware)
