from pathlib import Path

from typing import Literal

from pydantic import BaseModel, ConfigDict


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "0.1.0"
    host: Literal["127.0.0.1"] = "127.0.0.1"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    data_root: Path = Path(r"D:\LiteratureWorkbenchData")

    @property
    def database_path(self) -> Path:
        return self.data_root / "db" / "literature-workbench.sqlite3"

    @property
    def qwen_runtime_dir(self) -> Path:
        return self.data_root / "runtime" / "qwen"
