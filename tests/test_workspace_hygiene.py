from types import SimpleNamespace

from conftest import pytest_configure, pytest_sessionfinish


def test_successful_run_cleans_only_its_generated_directory(tmp_path):
    generated = tmp_path / 'generated'
    generated.mkdir()
    (generated / 'mock.sqlite3').write_text('synthetic test data')
    neighbor = tmp_path / 'keep'
    neighbor.mkdir()
    session = SimpleNamespace(config=SimpleNamespace(_egoassist_generated_basetemp=generated))
    pytest_sessionfinish(session, 0)
    assert not generated.exists()
    assert neighbor.is_dir()


def test_failed_run_and_explicit_basetemp_are_preserved(tmp_path):
    failed = tmp_path / 'failed'
    failed.mkdir()
    session = SimpleNamespace(config=SimpleNamespace(_egoassist_generated_basetemp=failed))
    pytest_sessionfinish(session, 1)
    assert failed.is_dir()
    explicit = SimpleNamespace(option=SimpleNamespace(basetemp=str(tmp_path)))
    pytest_configure(explicit)
    assert not hasattr(explicit, '_egoassist_generated_basetemp')
    pytest_sessionfinish(SimpleNamespace(config=explicit), 0)
    assert tmp_path.is_dir()
