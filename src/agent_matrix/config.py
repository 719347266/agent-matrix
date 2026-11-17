import os
from pathlib import Path
import tempfile
import yaml
from .models import Config


def load(path: Path) -> Config:
    return Config.model_validate(yaml.safe_load(path.read_text()) if path.exists() else {})


def save(path: Path, config: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.safe_dump(config.model_dump(), stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
