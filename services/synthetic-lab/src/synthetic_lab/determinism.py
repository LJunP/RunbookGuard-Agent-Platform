"""确定性伪随机。

不使用 random.Random：它的输出取决于调用顺序，一旦并发查询或新增信号，
整个剧本的数据就会全盘变化，「连续 3 次一致」立刻失效。
这里每个坐标独立派生子种子，因此任意查询顺序、任意并发都得到同一结果。
"""

from __future__ import annotations

import hashlib
import struct


def _u64(*parts: object) -> int:
    key = "|".join(str(p) for p in parts).encode("utf-8")
    return struct.unpack("<Q", hashlib.blake2b(key, digest_size=8).digest())[0]


def unit(seed: int, *coords: object) -> float:
    """返回 [0, 1) 区间内由坐标唯一决定的值。"""
    return _u64(seed, *coords) / 2**64


def jitter(seed: int, *coords: object, amplitude: float) -> float:
    """返回 [-amplitude, +amplitude] 区间内的确定性抖动。"""
    return (unit(seed, *coords) * 2.0 - 1.0) * amplitude


def pick(seed: int, options: list, *coords: object):
    if not options:
        raise ValueError("options must not be empty")
    return options[_u64(seed, *coords) % len(options)]
