import importlib.metadata
import ssl
from io import BytesIO

import certifi
import fsspec
import pytest
import urllib3
from filelock import FileLock, Timeout
from PIL import Image

from dependency_profiles import read_project_requirements, read_requirements

PATCHED = {
    "certifi": "2026.10.23",
    "filelock": "4.2.1",
    "fsspec": "2026.6.0",
    "pillow": "13.0.0",
    "urllib3": "2.8.0",
}


def test_security_pins_cover_every_install_path():
    manifests = [read_project_requirements("pyproject.toml")]
    for name in ("requirements.txt", "requirements-lock.txt", "requirements-lock-5090.txt",
                 "requirements-test.txt"):
        manifests.append(read_requirements(name)[0])
    for pins in manifests:
        assert {name: pins.get(name) for name in PATCHED} == PATCHED
    assert {name: importlib.metadata.version(name) for name in PATCHED} == PATCHED


def test_filelock_exclusion_and_release(tmp_path):
    path = tmp_path / "cache.lock"
    first, second = FileLock(path), FileLock(path)
    with first:
        with pytest.raises(Timeout):
            second.acquire(timeout=0)
    with second.acquire(timeout=0):
        assert second.is_locked


def test_fsspec_local_round_trip(tmp_path):
    path = tmp_path / "data.txt"
    with fsspec.open(str(path), "wt", encoding="utf-8") as handle:
        handle.write("original source paragraph")
    with fsspec.open(str(path), "rt", encoding="utf-8") as handle:
        assert handle.read() == "original source paragraph"


def test_pillow_lossless_round_trip():
    stream = BytesIO()
    Image.new("RGB", (3, 2), (12, 80, 140)).save(stream, format="PNG")
    stream.seek(0)
    with Image.open(stream) as restored:
        restored.load()
        assert restored.size == (3, 2)
        assert restored.getpixel((1, 1)) == (12, 80, 140)


def test_patched_certificate_bundle_and_urllib3_context():
    context = ssl.create_default_context(cafile=certifi.where())
    assert context.cert_store_stats()["x509_ca"] > 0
    with urllib3.PoolManager(ssl_context=context) as pool:
        assert pool.connection_pool_kw["ssl_context"].verify_mode == ssl.CERT_REQUIRED
