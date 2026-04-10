from abc import ABC, abstractmethod
from pathlib import Path

class Command(ABC):
    @staticmethod
    @abstractmethod
    def execute(args: list[str], repo_root: Path, model: str):
        pass