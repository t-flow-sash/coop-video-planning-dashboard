from __future__ import annotations

from typing import Any


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str,
        *,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.errors = errors
        super().__init__(detail)
