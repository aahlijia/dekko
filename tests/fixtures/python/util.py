def helper(x: int, y: int = 2) -> int:
    return x + y


class Config:
    def load(self, path: str) -> "Config":
        return self

    def validate(self) -> bool:
        return self.load("x") is not None
