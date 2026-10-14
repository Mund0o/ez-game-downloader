"""Minimal readers/writers for Valve's KeyValues formats.

* binary VDF (``userdata/<id>/config/shortcuts.vdf``)
* text VDF (``steamapps/libraryfolders.vdf``) — read only
"""

from __future__ import annotations

import re
import struct
from typing import Any

T_MAP, T_STRING, T_INT32, T_FLOAT, T_PTR, T_WSTRING, T_COLOR, T_UINT64, T_END = 0, 1, 2, 3, 4, 5, 6, 7, 8
T_END_ALT, T_INT64 = 11, 10


class VdfError(ValueError):
    pass


class Int32(int):
    """Marks a value to be written as a binary int32."""


def binary_loads(data: bytes) -> dict[str, Any]:
    pos = 0

    def cstr() -> str:
        nonlocal pos
        end = data.find(b"\x00", pos)
        if end < 0:
            raise VdfError("unterminated string")
        s = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    def read_map() -> dict[str, Any]:
        nonlocal pos
        out: dict[str, Any] = {}
        while True:
            if pos >= len(data):
                raise VdfError("unexpected end of data")
            t = data[pos]
            pos += 1
            if t in (T_END, T_END_ALT):
                return out
            key = cstr()
            if t == T_MAP:
                out[key] = read_map()
            elif t == T_STRING:
                out[key] = cstr()
            elif t in (T_INT32, T_PTR, T_COLOR):
                out[key] = Int32(struct.unpack_from("<i", data, pos)[0])
                pos += 4
            elif t == T_FLOAT:
                out[key] = struct.unpack_from("<f", data, pos)[0]
                pos += 4
            elif t in (T_UINT64,):
                out[key] = struct.unpack_from("<Q", data, pos)[0]
                pos += 8
            elif t == T_INT64:
                out[key] = struct.unpack_from("<q", data, pos)[0]
                pos += 8
            else:
                raise VdfError(f"unsupported binary VDF type {t} at {pos}")

    if not data:
        return {}
    result = read_map()
    return result


def binary_dumps(obj: dict[str, Any]) -> bytes:
    out = bytearray()

    def write_map(d: dict[str, Any]) -> None:
        for k, v in d.items():
            key = str(k).encode("utf-8") + b"\x00"
            if isinstance(v, dict):
                out.append(T_MAP)
                out.extend(key)
                write_map(v)
            elif isinstance(v, bool):
                out.append(T_INT32)
                out.extend(key)
                out.extend(struct.pack("<i", int(v)))
            elif isinstance(v, int):
                out.append(T_INT32)
                out.extend(key)
                out.extend(struct.pack("<i", v if -2**31 <= v < 2**31 else v - 2**32))
            elif isinstance(v, float):
                out.append(T_FLOAT)
                out.extend(key)
                out.extend(struct.pack("<f", v))
            else:
                out.append(T_STRING)
                out.extend(key)
                out.extend(str(v).encode("utf-8") + b"\x00")
        out.append(T_END)

    write_map(obj)
    return bytes(out)


_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|(//[^\n]*)')


def text_loads(text: str) -> dict[str, Any]:
    stack: list[dict[str, Any]] = [{}]
    key: str | None = None
    for m in _TOKEN.finditer(text):
        s, brace, comment = m.groups()
        if comment:
            continue
        if brace == "{":
            if key is None:
                raise VdfError("map without key")
            new: dict[str, Any] = {}
            stack[-1][key] = new
            stack.append(new)
            key = None
        elif brace == "}":
            if len(stack) == 1:
                raise VdfError("unbalanced braces")
            stack.pop()
        else:
            val = s.replace('\\"', '"').replace("\\\\", "\\")
            if key is None:
                key = val
            else:
                stack[-1][key] = val
                key = None
    return stack[0]
