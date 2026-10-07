class MirrorError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def as_dict(self):
        return {"error": {"code": self.code, "message": self.message}}
