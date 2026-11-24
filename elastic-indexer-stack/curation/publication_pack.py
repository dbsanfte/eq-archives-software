"""Write bounded packs of canonical Git trees without opening their children."""

import hashlib
import os
import struct
import tempfile
import zlib


class TreePack:
    def __init__(self, install, *, objects=4096, size=16 * 1024 * 1024):
        self.install = install
        self.object_limit = objects
        self.size_limit = size
        self.file = None
        self.ids = set()

    def add(self, tree):
        oid = hashlib.sha1(b'tree %d\0' % len(tree) + tree).hexdigest()
        if oid in self.ids:
            return oid
        if self.file is None:
            self.file = tempfile.TemporaryFile()
            self.file.write(b'\0' * 12)
        self.ids.add(oid)
        size = len(tree)
        first = (2 << 4) | (size & 15)
        size >>= 4
        header = bytearray([first | (128 if size else 0)])
        while size:
            current, size = size & 127, size >> 7
            header.append(current | (128 if size else 0))
        self.file.write(header)
        self.file.write(zlib.compress(tree, 1))
        if len(self.ids) >= self.object_limit or self.file.tell() >= self.size_limit:
            self.flush()
        return oid

    def flush(self):
        if self.file is None:
            return
        with self.file as pack:
            pack.seek(0)
            pack.write(struct.pack('>4sII', b'PACK', 2, len(self.ids)))
            pack.seek(0)
            checksum = hashlib.sha1()
            while chunk := pack.read(256 * 1024):
                checksum.update(chunk)
            pack.seek(0, os.SEEK_END)
            pack.write(checksum.digest())
            pack.seek(0)
            self.install(pack)
        self.file = None
        self.ids.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.flush()
        finally:
            if self.file is not None:
                self.file.close()
