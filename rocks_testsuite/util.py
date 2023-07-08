from enum import Enum


class ResultCode(str, Enum):
    Inconclusive = "Inconclusive"
    NotApplicable = "N/A"


ResultsType = dict[str, dict[str, bool | ResultCode]]


class TestFailure:
    def __init__(self, comment: str):
        self.comment = comment


class TestInconclusive:
    def __init__(self, comment: str):
        self.comment = comment
