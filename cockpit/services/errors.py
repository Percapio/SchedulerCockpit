class PrintError(Exception):
    def __init__(self, reason: str, detail: dict) -> None:
        super().__init__(f"Print Error: {reason}")
        self.reason = reason
        self.detail = detail
