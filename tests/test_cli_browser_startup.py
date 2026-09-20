import sys

from aiohttp import web
import pytest

import run_egoassist as cli


@pytest.mark.parametrize("open_browser", [True, False])
def test_browser_waits_for_server_ready(tmp_path, monkeypatch, open_browser):
    opened = []
    app = object()
    monkeypatch.setattr(cli, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_env", lambda *_: None)
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **_: None)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(sys, "argv", ["run_egoassist.py", "--port", "8783"] + (["--open"] if open_browser else []))

    class ImmediateThread:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args
            assert daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(cli.threading, "Thread", ImmediateThread)

    def create_app(*args):
        assert opened == []
        return app

    def run_app(actual_app, **kwargs):
        assert actual_app is app
        assert opened == []
        kwargs["print"]("server ready")
        assert opened == (["http://127.0.0.1:8783"] if open_browser else [])

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(web, "run_app", run_app)
    cli.main()


def test_failed_startup_does_not_open_browser(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_env", lambda *_: None)
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **_: None)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(sys, "argv", ["run_egoassist.py", "--open"])
    monkeypatch.setattr(cli, "create_app", lambda *_: object())

    def fail(*args, **kwargs):
        raise OSError("port already in use")

    monkeypatch.setattr(web, "run_app", fail)
    with pytest.raises(OSError, match="port already in use"):
        cli.main()
    assert opened == []
