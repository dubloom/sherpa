from abc import ABC, abstractmethod

class Command(ABC):
    @staticmethod
    @abstractmethod
    def execute(args: list[str], model: str):
        pass