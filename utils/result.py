from dataclasses import dataclass
from typing import Generic, TypeVar, Union

T = TypeVar("T")
E = TypeVar("E")

@dataclass
class Ok(Generic[T]):
    value: T

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False

    def unwrap(self) -> T:
        return self.value

    def unwrap_err(self) -> E:
        raise ValueError(f"Called unwrap_err on Ok: {self.value}")

@dataclass
class Err(Generic[E]):
    error: E

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True

    def unwrap(self) -> T:
        raise ValueError(f"Called unwrap on Err: {self.error}")

    def unwrap_err(self) -> E:
        return self.error

Result = Union[Ok[T], Err[E]]

def ok(value: T) -> Ok[T]:
    return Ok(value)

def err(error: E) -> Err[E]:
    return Err(error)