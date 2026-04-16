from abc import ABC, abstractmethod
from pathlib import Path

from sherpa.config import SherpaConfig

class Command(ABC):
    @staticmethod
    @abstractmethod
    def execute(args: list[str], repo_root: Path, config: SherpaConfig):
        pass