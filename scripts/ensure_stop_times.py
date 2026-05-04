import os
from pathlib import Path
from urllib.request import urlretrieve


EXPECTED_HEADER = "trip_id,arrival_time,departure_time,stop_id,stop_sequence"
DEFAULT_URL = (
    "https://media.githubusercontent.com/media/ProccyBoi/Metroboard/main/"
    "gtfs/stop_times.txt"
)


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
    url = os.getenv("STOP_TIMES_URL", DEFAULT_URL)
    path.parent.mkdir(parents=True, exist_ok=True)

    if has_valid_header(path):
        print(f"stop_times ok: {path}")
        return

    tmp = path.with_suffix(path.suffix + ".download")
    print(f"downloading stop_times from {url}")
    urlretrieve(url, tmp)
    if not has_valid_header(tmp):
        raise RuntimeError(f"downloaded stop_times has invalid header: {url}")
    tmp.replace(path)
    print(f"stop_times restored: {path}")


if __name__ == "__main__":
    main()
