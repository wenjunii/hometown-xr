import sys
from types import SimpleNamespace

import config
import main


class _FakeCuda:
    def __init__(self, name="NVIDIA GeForce RTX 5090", capability=(12, 0)):
        self._name = name
        self._capability = capability

    @staticmethod
    def is_available():
        return True

    def get_device_name(self, _index):
        return self._name

    def get_device_capability(self, _index):
        return self._capability


def _fake_torch(cuda_runtime="13.0", name="NVIDIA GeForce RTX 5090", capability=(12, 0)):
    return SimpleNamespace(
        __version__="2.12.1+cu130",
        version=SimpleNamespace(cuda=cuda_runtime),
        cuda=_FakeCuda(name=name, capability=capability),
    )


def test_5090_profile_has_conservative_tracked_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HARDWARE_OVERRIDE_PATH", tmp_path / "missing.json")

    profile = config.get_hardware_profile("5090")

    assert profile == config.HardwareProfile("5090", 7, 200, 2_400, 512, "fp32")


def test_auto_detection_selects_5090(monkeypatch):
    monkeypatch.setenv("HOMETOWN_XR_PROFILE", "auto")
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())

    assert config.detect_hardware_profile() == "5090"


def test_5090_doctor_accepts_blackwell_cuda_runtime(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    monkeypatch.setattr(
        main,
        "get_hardware_profile",
        lambda _name: config.HARDWARE_PROFILES["5090"],
    )

    assert main.doctor("5090") == 0


def test_5090_doctor_rejects_legacy_cuda_runtime(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda_runtime="12.1"))
    monkeypatch.setattr(
        main,
        "get_hardware_profile",
        lambda _name: config.HARDWARE_PROFILES["5090"],
    )

    assert main.doctor("5090") == 1
    assert "requires a PyTorch CUDA 12.8+ build" in capsys.readouterr().out


def test_legacy_doctor_rejects_wrong_gpu_profile(monkeypatch, capsys):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        _fake_torch(
            cuda_runtime="12.1",
            name="NVIDIA GeForce RTX 4090",
            capability=(8, 9),
        ),
    )
    monkeypatch.setattr(
        main,
        "get_hardware_profile",
        lambda _name: config.HARDWARE_PROFILES["3080"],
    )

    assert main.doctor("3080") == 1
    assert "selected a different GPU" in capsys.readouterr().out
