"""Un equipo compuesto para trabajar sobre un workspace tiene que poder leerlo.

Medido en producción el 2026-09-19 (rev 0000130, plan c7ab4b24, sesión
autonoma-001): el Router escaló con ``WORKSPACE_ID=multi-agent-custom-
automation-engine-solution-accelerator`` y compuso
``RepositoryAuditAgent(mcp=False) ValidationAgent(mcp=False)
DevSecOpsAuditAgent(mcp=False)``. En los cinco minutos del run hubo CERO
llamadas a herramientas de workspace, y la "auditoría" resultante describía un
``config.yaml``, un ``requirements.txt`` con ``pyaudio``/``speechrecognition`` y
un ``Dockerfile`` que no existen en el árbol. Sin herramientas no leyó otro
árbol: no leyó ninguno.

La causa está en el esquema de ``run_plan``: ``use_mcp`` se describe como "true
ONLY if it needs external systems or live data" y no figura en ``required``, así
que para auditar el repositorio del propio usuario el Router lo omite. El
docstring de ``_team_from_router_roster`` ya fija la regla —"Factory constraints
are re-checked HERE, in code (the prompt orients the Router; it guarantees
nothing)"—; a ese re-chequeo le faltaba el acceso al workspace.
"""

import importlib

import pytest

router = importlib.import_module("v4.api.router")

WS = "multi-agent-custom-automation-engine-solution-accelerator"

# El roster real de la 0000130, tal cual lo emitió el Router: sin use_mcp.
ROSTER = [
    {
        "name": "RepositoryAuditAgent",
        "description": "Audita la estructura del repositorio",
        "system_message": "Revisa el árbol del proyecto.",
        "use_reasoning": True,
    },
    {
        "name": "ValidationAgent",
        "description": "Ejecuta validaciones no destructivas",
        "system_message": "Corre las pruebas y los linters.",
        "coding_tools": True,
    },
    {
        "name": "DevSecOpsAuditAgent",
        "description": "Revisa CI/CD y seguridad",
        "system_message": "Revisa los workflows.",
        "use_reasoning": True,
    },
]
ROSTER_NAMES = [a["name"] for a in ROSTER]


class FakeStore:
    """Sólo la capa Cosmos: el TeamService y el modelo son los reales."""

    def __init__(self):
        self.saved = []

    async def get_team(self, team_id):
        return None

    async def add_team(self, team_config):
        self.saved.append(team_config)

    async def update_team(self, team_config):
        self.saved.append(team_config)


async def compose(roster, workspace_id):
    return await router._team_from_router_roster(
        roster, "audita el proyecto", "u1", FakeStore(), workspace_id
    )


def by_name(team):
    return {a.name: a for a in team.agents}


@pytest.mark.asyncio
async def test_a_roster_composed_for_a_workspace_can_read_it():
    team = await compose(ROSTER, WS)

    agents = by_name(team)
    assert [a.name for a in team.agents] == [
        "RepositoryAuditAgent",
        "ValidationAgent",
        "DevSecOpsAuditAgent",
        "ProxyAgent",
    ]
    assert all(agents[n].use_mcp for n in ROSTER_NAMES)


@pytest.mark.asyncio
async def test_the_clarification_channel_stays_a_channel():
    """ProxyAgent es el canal humano; darle herramientas lo vuelve un agente."""
    team = await compose(ROSTER, WS)

    assert by_name(team)["ProxyAgent"].use_mcp is False


@pytest.mark.asyncio
async def test_without_a_workspace_nothing_is_granted():
    """Sin workspace montado no hay contrato que conformar: no se concede nada."""
    team = await compose(ROSTER, None)

    assert not any(a.use_mcp for a in team.agents)


@pytest.mark.asyncio
async def test_a_partially_sighted_roster_is_not_good_enough():
    """No alcanza con que UNO vea: tiene que ver el que va a mirar el árbol.

    Roster medido el 2026-09-19 en el mismo camino real: el Router concedió
    ``use_mcp`` únicamente a ``CloudDeliveryAgent`` y dejó ciego a
    ``RepositoryForensicsAgent`` —cuyo ``system_message`` dice "verificas ruta,
    Git, rama, commit, árbol real, manifiestos"— con ``coding_tools``, es decir
    con un sandbox de code interpreter vacío en vez del repositorio. El manager
    Magentic reparte los pasos por nombre y descripción, sin saber quién tiene
    herramientas.
    """
    roster = [
        {
            "name": "RepositoryForensicsAgent",
            "description": "Inspecciona repositorios",
            "system_message": "Verificas ruta, Git, rama, commit y árbol real.",
            "coding_tools": True,
        },
        {
            "name": "CloudDeliveryAgent",
            "description": "Audita infraestructura",
            "system_message": "Revisas IaC y pipelines.",
            "use_mcp": True,
        },
    ]

    agents = by_name(await compose(roster, WS))

    assert agents["RepositoryForensicsAgent"].use_mcp is True
    assert agents["CloudDeliveryAgent"].use_mcp is True
