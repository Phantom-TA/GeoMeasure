class IngestError(Exception):
    """A problem with the uploaded file itself; reported to the client, never retried."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
