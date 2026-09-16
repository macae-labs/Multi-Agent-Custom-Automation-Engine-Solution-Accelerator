"""Sonda de INC-2026-004 (SIG-sys-modules-package-shadowed-by-tests-dir).

Plugin de pytest que detecta, test por test, si algún módulo del producto o
alguno de sus atributos clave cambia de identidad (id()) durante el lote:
eso es exactamente lo que hacían los tests que instalaban Mocks en
sys.modules, borraban módulos reales o cargaban el producto por ruta de
archivo bajo un segundo nombre. Señal sana: ninguna línea MODTRACK.

Es gate: con una o más señales la sesión de pytest termina con exit 1 aunque
todos los tests pasen (test.yml lo carga con -p). Sana: ninguna línea MODTRACK.

Uso (desde src/backend):
  PYTHONPATH=$PWD/../../docs/incidents/probes:$PWD/../..:$PWD \\
    uv run python -m pytest ../../src/tests/backend -q -p module_identity_tracker
"""

import sys
import types

import pytest

MODS = (
    "v4.config.settings",
    "v4.models.messages",
    "common.config.app_config",
    "v4.callbacks.response_handlers",
    "common.database.cosmosdb",
    "v4.orchestration.orchestration_manager",
    "v4.common.services.team_service",
    "auth.auth_utils",
    "app",
)
ATTRS = (
    ("v4.config.settings", "logger"),
    ("v4.config.settings", "config"),
    ("v4.config.settings", "connection_config"),
    ("v4.config.settings", "orchestration_config"),
    ("v4.config.settings", "ConnectionConfig"),
    ("common.config.app_config", "config"),
)


def _snapshot():
    snap = {m: id(sys.modules[m]) for m in MODS if m in sys.modules}
    for m, a in ATTRS:
        mod = sys.modules.get(m)
        if mod is not None:
            snap[f"{m}.{a}"] = id(getattr(mod, a, None))
    return snap


VIOLATIONS: list[str] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    before = _snapshot()
    yield
    after = _snapshot()
    for key in sorted(set(before) | set(after)):
        if key in before and key in after and before[key] != after[key]:
            VIOLATIONS.append(f"{item.nodeid}: {key} REEMPLAZADO")
        elif key in before and key not in after:
            VIOLATIONS.append(f"{item.nodeid}: {key} BORRADO")
        elif key in after and key in MODS and not isinstance(sys.modules.get(key), types.ModuleType):
            # Un módulo del producto que aparece por primera vez y NO es un módulo:
            # alguien lo instaló como Mock antes de que el producto lo importara.
            VIOLATIONS.append(f"{item.nodeid}: {key} INSTALADO COMO {type(sys.modules.get(key)).__name__}")
        else:
            continue
        print(f"\nMODTRACK {VIOLATIONS[-1]}", file=sys.stderr)


def pytest_terminal_summary(terminalreporter):
    if VIOLATIONS:
        terminalreporter.section("MODTRACK: módulos del producto reemplazados o borrados durante el lote")
        for line in VIOLATIONS:
            terminalreporter.write_line(line)


def pytest_sessionfinish(session, exitstatus):
    if VIOLATIONS and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
