import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TAG = 'Signature: 8a477f597d28d172789f06886806bc55\n'


@pytest.fixture
def cleanup_project(tmp_path):
    shell = shutil.which('pwsh')
    if not shell:
        pytest.skip('PowerShell is not installed')
    root = tmp_path / 'test project'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / 'scripts/cleanup.ps1', root / 'scripts')
    return root, shell


def invoke(project, *arguments):
    root, shell = project
    return subprocess.run(
        [shell, '-NoProfile', '-File', str(root / 'scripts/cleanup.ps1'), *arguments],
        cwd=root.parent, capture_output=True, text=True, timeout=30,
    )


def put(root, name, text='cache\n'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def test_preview_is_read_only_and_excludes_dependencies_and_data(cleanup_project):
    root, _ = cleanup_project
    files = [
        put(root, '__pycache__/main.cpython-311.pyc'),
        put(root, 'tests/__pycache__/test_main.cpython-311-pytest-9.1.1.pyc'),
        put(root, '.pytest_cache/CACHEDIR.TAG', TAG),
        put(root, '.pytest_cache/v/cache/nodeids', '[]\n'),
        put(root, '.ruff_cache/CACHEDIR.TAG', TAG),
        put(root, '.ruff_cache/0.17.0/12345'),
    ]
    protected = [
        put(root, 'data/models/model.bin'), put(root, '.venv/keep.txt'),
        put(root, 'data/output/stories.md'), put(root, 'data/evidence-local/proof.json'),
        put(root, 'data/__pycache__/record.cpython-311.pyc'),
        put(root, 'reference.txt'),
    ]
    before = {path: path.read_bytes() for path in files + protected}

    run = invoke(cleanup_project)

    assert run.returncode == 0, run.stdout + run.stderr
    report = json.loads(run.stdout)
    assert not report['applied'] and not report['recycle_bin_requested']
    assert report['bytes'] == sum(path.stat().st_size for path in files)
    assert {item['relative'] for item in report['targets']} == {
        '__pycache__', 'tests/__pycache__', '.pytest_cache', '.ruff_cache',
    }
    assert {path: path.read_bytes() for path in before} == before


def test_apply_requires_successful_verification(cleanup_project):
    root, _ = cleanup_project
    cache = put(root, '__pycache__/main.cpython-311.pyc')
    run = invoke(cleanup_project, '-Apply')
    assert run.returncode != 0
    assert 'Apply requires -Verified' in run.stdout + run.stderr
    assert cache.exists()


@pytest.mark.parametrize('name, marker', [
    ('__pycache__/story.md', False),
    ('.pytest_cache/story.md', True),
    ('.ruff_cache/0.17.0/story.md', True),
    ('.pytest_cache/v/cache/nodeids', False),
])
def test_all_targets_are_validated_before_any_removal(cleanup_project, name, marker):
    root, _ = cleanup_project
    good = put(root, 'tests/__pycache__/test_main.cpython-311.pyc')
    unsafe = put(root, name)
    if marker:
        put(root, f'{name.split("/")[0]}/CACHEDIR.TAG', TAG)

    run = invoke(cleanup_project, '-Apply', '-Verified')

    assert run.returncode != 0
    assert 'Unrecognized cache' in run.stdout + run.stderr or 'Cache marker' in run.stdout + run.stderr
    assert good.exists() and unsafe.exists()


def test_tracked_cache_files_are_protected(cleanup_project):
    root, _ = cleanup_project
    cache = put(root, '__pycache__/main.cpython-311.pyc')
    subprocess.run(['git', 'init', '--quiet'], cwd=root, check=True)
    subprocess.run(['git', 'add', '-f', '--', '__pycache__'], cwd=root, check=True)

    run = invoke(cleanup_project, '-Apply', '-Verified')

    assert run.returncode != 0
    assert 'Cleanup refuses tracked' in run.stdout + run.stderr
    assert cache.exists()


def test_linked_cache_directory_is_protected(cleanup_project, tmp_path):
    root, shell = cleanup_project
    outside = tmp_path / 'outside'
    sentinel = put(outside, 'keep.txt')
    link = root / '__pycache__'
    if os.name == 'nt':
        environment = dict(os.environ, CACHE_LINK=str(link), CACHE_TARGET=str(outside))
        subprocess.run(
            [shell, '-NoProfile', '-Command',
             'New-Item -ItemType Junction -Path $env:CACHE_LINK -Target $env:CACHE_TARGET | Out-Null'],
            env=environment, check=True, capture_output=True, text=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)

    run = invoke(cleanup_project, '-Apply', '-Verified')

    assert run.returncode != 0
    assert 'Cleanup refuses linked' in run.stdout + run.stderr
    assert sentinel.exists()


def test_crawler_lock_blocks_cleanup(cleanup_project):
    root, _ = cleanup_project
    cache = put(root, '__pycache__/main.cpython-311.pyc')
    put(root, 'data/.crawler.lock')
    run = invoke(cleanup_project, '-Apply', '-Verified')
    assert run.returncode != 0
    assert 'crawler lock exists' in run.stdout + run.stderr
    assert cache.exists()


def test_apply_uses_recycle_bin_or_refuses_without_permanent_fallback(cleanup_project):
    root, _ = cleanup_project
    cache = put(root, '__pycache__/main.cpython-311.pyc')
    protected = put(root, 'data/output/keep.md')
    run = invoke(cleanup_project, '-Apply', '-Verified')
    if os.name == 'nt' and run.returncode == 0:
        report = json.loads(run.stdout)
        assert report['applied'] and report['recycle_bin_requested']
        assert not cache.exists()
    else:
        assert run.returncode != 0
        assert 'interactive Windows Recycle Bin' in run.stdout + run.stderr
        assert cache.exists()
    assert protected.exists()
