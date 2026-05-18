from dataclasses import dataclass


@dataclass(frozen=True)
class Pagination:
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page
