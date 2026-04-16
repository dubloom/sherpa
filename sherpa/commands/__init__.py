from enum import Enum

class Commands(str, Enum):
    REVIEW = "review"
    FIX = "fix"
    COMMIT = "commit"
    ADDRESS = "address"
    CONFIG = "config"