from __future__ import annotations

from enum import Enum


NATIVE_POINT_LIMIT = 500_000
HEAVY_POINT_LIMIT = 2_000_000


class TakeBudgetTier(str, Enum):
    NATIVE = "NATIVE"
    HEAVY = "HEAVY"
    SPLIT_RECOMMENDED = "SPLIT_RECOMMENDED"


def classify_take_size(point_count: int) -> TakeBudgetTier:
    if type(point_count) is not int or point_count < 0:
        raise ValueError("point_count must be a non-negative integer")
    if point_count < NATIVE_POINT_LIMIT:
        return TakeBudgetTier.NATIVE
    if point_count <= HEAVY_POINT_LIMIT:
        return TakeBudgetTier.HEAVY
    return TakeBudgetTier.SPLIT_RECOMMENDED
