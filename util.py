import os
import json
from pathlib import Path

def _get_filepath(dir: str|list[str], filename: str) -> str:
    path = os.path.join(*dir, filename) if isinstance(dir, list) else os.path.join(dir, filename)
    return Path(path).as_posix()

def read_file(dir: str|list[str], filename: str) -> str:
    with open(_get_filepath(dir, filename), "r") as f:
        content = f.read()
    return content

def write_file(dir: str|list[str], filename: str, content: str) -> None:
    with open(_get_filepath(dir, filename), "w") as f:
        f.write(content)

def read_dict(dir: str|list[str], filename: str) -> dict[str, str]:
    with open(_get_filepath(dir, filename), "r") as f:
        content = json.load(f)
    return content

def write_dict(dir: str|list[str], filename: str, content: dict|list) -> None:
    with open(_get_filepath(dir, filename), "w") as f:
        json.dump(content, f)