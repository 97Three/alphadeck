"""Use the system ICU collation rules for locale-sensitive upstream snapshot hashes."""

import ctypes
import ctypes.util
import re
import threading
from functools import lru_cache


class Collator:
    def __init__(self, locale):
        library = ctypes.util.find_library("icui18n")
        if not library:
            raise RuntimeError("Evaluation snapshot collation requires the system ICU library.")
        self.library = ctypes.CDLL(library)
        match = re.search(r"(?:\.so\.|\.)([0-9]+)(?:\.|$)", library)
        suffix = "_" + match[1] if match else ""
        self.open = getattr(self.library, "ucol_open" + suffix)
        self.open.argtypes, self.open.restype = (
            [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int32)],
            ctypes.c_void_p,
        )
        self.sort = getattr(self.library, "ucol_getSortKey" + suffix)
        self.sort.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int32,
        ]
        self.sort.restype = ctypes.c_int32
        self.close = getattr(self.library, "ucol_close" + suffix)
        self.close.argtypes, self.close.restype = [ctypes.c_void_p], None
        status = ctypes.c_int32(0)
        self.handle = self.open(locale.encode("ascii"), ctypes.byref(status))
        if status.value > 0 or not self.handle:
            raise RuntimeError("Unable to initialize evaluation snapshot collation.")
        self.lock = threading.Lock()

    def key(self, value):
        encoded = value.encode("utf-16-le", "surrogatepass")
        source = (ctypes.c_uint16 * (len(encoded) // 2)).from_buffer_copy(encoded)
        with self.lock:
            size = self.sort(self.handle, source, len(source), None, 0)
            buffer = (ctypes.c_uint8 * size)()
            self.sort(self.handle, source, len(source), buffer, size)
            return bytes(buffer)

    def __del__(self):
        if getattr(self, "handle", None):
            self.close(self.handle)
            self.handle = None


@lru_cache(maxsize=8)
def collator(locale="en-US"):
    return Collator(locale)
