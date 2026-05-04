import gzip
import os
import shutil
from pathlib import Path
from urllib.request import urlretrieve


EXPECTED_HEADER = "trip_id,arrival_time,departure_time,stop_id,stop_sequence"


def has_valid_header(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return f.readline().strip().startswith(EXPECTED_HEADER)
    except UnicodeDecodeError:
        return False


def main() -> None:
    path = Path(os.getenv("STOP_TIMES_PATH", "gtfs/stop_times.txt"))
    gz_path = Path(os.getenv("STOP_TIMES_GZ_PATH", str(path) + ".gz"))
    url = os.getenv("STOP_TIMES_URL", "")
    path.parent.mkdir(parents=True, exist_ok=True)

    if has_valid_header(path):
        print(f"stop_times ok: {path}")
        return

    if gz_path.exists():
        print(f"decompressing stop_times from {gz_path}")
        tmp = path.with_suffix(path.suffix + ".inflate")
        with gzip.open(gz_path, "rb") as f_in, tmp.open("wb") as f_out:
            shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
        if not has_valid_header(tmp):
            raise RuntimeError(f"decompressed stop_times has invalid header: {gz_path}")
        tmp.replace(path)
        print(f"stop_times restored: {path}")
        return

    if not url:
        raise RuntimeError(f"stop_times is invalid and no compressed fallback exists: {path}")

    tmp = path.with_suffix(path.suffix + ".download")
    print(f"downloading stop_times from {url}")
    urlretrieve(url, tmp)
    if not has_valid_header(tmp):
        raise RuntimeError(f"downloaded stop_times has invalid header: {url}")
    tmp.replace(path)
    print(f"stop_times restored: {path}")


if __name__ == "__main__":
    main()
