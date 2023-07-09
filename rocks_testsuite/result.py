class ResultCode(dict):  # dict so it's JSON-serializable
    def __init__(self, comment: str):
        self["code"] = type(self).__name__
        self["comment"] = comment

    def __repr__(self):
        return f"<{self['code']} '{self['comment']}'>"


class TestFailure(ResultCode):
    ...


class TestInconclusive(ResultCode):
    ...


class TestNotApplicable(ResultCode):
    ...


TestResult = bool | ResultCode
TestResults = dict[str, TestResult]
