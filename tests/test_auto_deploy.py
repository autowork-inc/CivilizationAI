"""scripts/auto_deploy.py: the health check must not call the finished one-shot ``migrate`` service a failure. The module needs paramiko at import
time (not installed in every environment), so a stand-in module is put in ``sys.modules`` for the import."""

from __future__ import annotations

import importlib
import sys
import types

import pytest


@pytest.fixture
def deploy(monkeypatch):
    if "paramiko" not in sys.modules:
        monkeypatch.setitem(sys.modules, "paramiko", types.SimpleNamespace(SSHClient=object, SSHException=Exception, AuthenticationException=Exception))
    return importlib.import_module("scripts.auto_deploy")


@pytest.mark.parametrize(
    ("service", "state", "status", "expected"),
    [
        ("migrate", "exited", "Exited (0) 12 seconds ago", True),  # the one-shot migration service finished: success
        ("migrate", "exited", "Exited (1) 12 seconds ago", False),  # ... but a failed migration is a failed deploy
        ("migrate", "exited", "Exited (137) 3 seconds ago", False),
        ("migrate", "running", "Up 5 seconds", False),  # still migrating after the wait: not done
        ("migrate", "restarting", "Restarting (1) 2 seconds ago", False),
        ("bot", "running", "Up 20 seconds", True),
        ("web", "running", "Up 20 seconds (healthy)", True),
        ("web", "running", "Up 20 seconds (unhealthy)", False),
        ("worker", "running", "Restarting (1) 4 seconds ago", False),
        ("bot", "exited", "Exited (0) 5 seconds ago", False),  # only the one-shot service may stop
        ("beat", "exited", "Exited (1) 5 seconds ago", False),
    ],
)
def test_service_health_rules(deploy, service, state, status, expected):
    assert deploy.service_ok(service, state, status) is expected


class FakeRemote:
    docker = "docker"

    def __init__(self, ps: str, logs: str = "") -> None:
        self.ps, self.logs = ps, logs

    def run(self, cmd: str, **kw):
        return 0, (self.ps if " ps " in cmd else self.logs)


def test_the_health_check_passes_with_a_finished_migrate_container(deploy, monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    ps = "migrate|exited|Exited (0) 30 seconds ago\nbot|running|Up 20 seconds\nweb|running|Up 20 seconds (healthy)"
    assert deploy.health_check(FakeRemote(ps), "~/ai_civ", 0) is True


def test_the_health_check_fails_when_migrate_failed_or_a_service_is_down(deploy, monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    assert deploy.health_check(FakeRemote("migrate|exited|Exited (1) 3 seconds ago\nbot|running|Up 20 seconds"), "d", 0) is False
    assert deploy.health_check(FakeRemote("migrate|exited|Exited (0) 3 seconds ago\nbot|exited|Exited (0) 2 seconds ago"), "d", 0) is False
