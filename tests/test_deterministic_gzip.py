import gzip
import io

from deterministic_gzip import gzip_binary_writer


class _NamedBuffer(io.BytesIO):
    def __init__(self, name):
        super().__init__()
        self.name = name


def _compressed(name):
    raw = _NamedBuffer(name)
    with gzip_binary_writer(raw) as handle:
        handle.write(b"same durable payload")
    return raw.getvalue()


def test_gzip_writer_omits_temporary_filename_and_is_byte_stable():
    first = _compressed("artifact.jsonl.gz.100.tmp")
    second = _compressed("artifact.jsonl.gz.200.tmp")

    assert first == second
    assert first[3] & gzip.FNAME == 0
    assert gzip.decompress(first) == b"same durable payload"
