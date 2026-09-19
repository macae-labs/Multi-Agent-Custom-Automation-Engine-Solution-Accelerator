import sys
from pathlib import Path


# Add the backend path to sys.path so we can import v4 modules
backend_path = Path(__file__).parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# El guardián que había aquí —un ``pytest.skip`` de módulo si
# ``v4.orchestration.human_approval_manager`` venía mockeado— cubría una
# contaminación que ya no existe: ``test_orchestration_manager.py`` instala sus
# dobles con ``patch.dict(sys.modules, ...)``, con alcance. Lo único que podía
# hacer era borrar estos ocho tests sin que nada lo dijera.
from v4.models.models import MPlan
from v4.orchestration.human_approval_manager import HumanApprovalMagenticManager

#
# Helper dummies to simulate the minimal shape required by plan_to_obj
#


class _Obj:
    """El ledger real entrega mensajes del framework, y esos exponen ``.text``.

    ``_MagenticTaskLedger(facts=facts_msg, plan=plan_msg)`` en
    ``agent_framework_orchestrations/_magentic.py``, que lee ``plan_msg.text``.
    El doble exponía ``.content``: ``plan_to_obj`` caía al ``getattr(..., "")``
    y devolvía un plan de cero pasos sin que nada fallara.
    """

    def __init__(self, text: str):
        self.text = text


class DummyLedger:
    def __init__(self, plan_content: str, facts_content: str = ""):
        self.plan = _Obj(plan_content)
        self.facts = _Obj(facts_content)


class DummyContext:
    def __init__(self, task: str, participant_descriptions: dict[str, str]):
        self.task = task
        self.participant_descriptions = participant_descriptions


def _make_manager():
    """
    Create a HumanApprovalMagenticManager instance without calling its __init__
    (avoids needing the full agent framework  dependencies for this focused unit test).
    """
    return HumanApprovalMagenticManager.__new__(HumanApprovalMagenticManager)


def test_plan_to_obj_basic_parsing():
    plan_text = """
- **ProductAgent** to provide detailed information about the company's current products.
- **MarketingAgent** to gather relevant market positioning insights, key messaging strategies.
- **MarketingAgent** to draft an initial press release outline based on the product details.
- **ProductAgent** to review the press release outline for technical accuracy and completeness of product details.
- **MarketingAgent** to finalize the press release draft incorporating the ProductAgent’s feedback.
- **ProxyAgent** to step in and request additional clarification or missing details from ProductAgent and MarketingAgent.
"""
    ctx = DummyContext(
        task="Analyze Q4 performance",
        participant_descriptions={
            "ProductAgent": "Provide product info",
            "MarketingAgent": "Handle marketing",
            "ProxyAgent": "Ask user for missing info",
        },
    )
    ledger = DummyLedger(plan_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)

    assert isinstance(mplan, MPlan)
    assert mplan.user_request == "Analyze Q4 performance"
    assert len(mplan.steps) == 6

    agents = [s.agent for s in mplan.steps]
    assert agents == [
        "ProductAgent",
        "MarketingAgent",
        "MarketingAgent",
        "ProductAgent",
        "MarketingAgent",
        "ProxyAgent",
    ]

    actions = [s.action for s in mplan.steps]
    assert (
        "to provide detailed information about the company's current products"
        in actions[0]
    )
    assert (
        "to gather relevant market positioning insights, key messaging strategies"
        in actions[1].lower()
    )
    assert (
        "to draft an initial press release outline based on the product details"
        in actions[2]
    )
    assert (
        "to review the press release outline for technical accuracy and completeness of product details"
        in actions[3]
    )
    assert (
        "to finalize the press release draft incorporating the productagent’s feedback"
        in actions[4].lower()
    )
    assert (
        "to step in and request additional clarification or missing details from productagent and marketingagent"
        in actions[5].lower()
    )


def test_plan_to_obj_ignores_non_bullet_lines_and_uses_fallback():
    plan_text = """
Introduction line that should be ignored
- **ResearchAgent** to collect competitor pricing
Some trailing commentary
- finalize compiled dataset
"""
    ctx = DummyContext(
        task="Compile competitive pricing dataset",
        participant_descriptions={
            "ResearchAgent": "Collect data",
        },
    )
    ledger = DummyLedger(plan_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)

    # Only 2 bullet lines
    assert len(mplan.steps) == 2
    assert mplan.steps[0].agent == "ResearchAgent"
    # Second bullet has no recognizable agent => fallback
    assert mplan.steps[1].agent == "MagenticAgent"
    assert "finalize compiled dataset" in mplan.steps[1].action.lower()


def test_plan_to_obj_resets_agent_each_line():
    plan_text = """
- **ResearchAgent** to gather initial statistics
- finalize normalizing collected values
"""
    ctx = DummyContext(
        task="Normalize stats",
        participant_descriptions={
            "ResearchAgent": "Collect data",
        },
    )
    ledger = DummyLedger(plan_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)

    assert len(mplan.steps) == 2
    assert mplan.steps[0].agent == "ResearchAgent"
    # Ensure no leakage of previous agent
    assert mplan.steps[1].agent == "MagenticAgent"


def test_plan_to_obj_keeps_a_line_ending_in_colon_intact():
    plan_text = """
- **ResearchAgent** to gather quarterly metrics:
"""
    ctx = DummyContext(
        task="Quarterly metrics",
        participant_descriptions={
            "ResearchAgent": "Collect metrics",
        },
    )
    ledger = DummyLedger(plan_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)

    # Expect 1 step
    assert len(mplan.steps) == 1
    # El xfail que cubría esto afirmaba una duplicación por el ':' final. Medido
    # contra el converter real: action == "to gather quarterly metrics:", sin
    # duplicar. El fallo venía del doble, que entregaba un plan vacío.
    assert mplan.steps[0].action.count("gather quarterly metrics") == 1


def test_plan_to_obj_empty_or_whitespace_plan():
    plan_text = "   \n \n"
    ctx = DummyContext(
        task="Empty plan test",
        participant_descriptions={
            "AgentA": "A",
        },
    )
    ledger = DummyLedger(plan_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)
    assert len(mplan.steps) == 0
    assert mplan.user_request == "Empty plan test"


def test_plan_to_obj_multiple_agents_case_insensitive():
    plan_text = """
- **researchagent** to collect raw feeds
- **ANALYSISAGENT** to process raw feeds
"""
    ctx = DummyContext(
        task="Case insensitivity test",
        participant_descriptions={
            "ResearchAgent": "Collect",
            "AnalysisAgent": "Process",
        },
    )
    ledger = DummyLedger(plan_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)
    assert [s.agent for s in mplan.steps] == ["ResearchAgent", "AnalysisAgent"]


def test_plan_to_obj_facts_copied():
    plan_text = "- **ResearchAgent** to gather X"
    facts_text = "Known constraints: Budget capped."
    ctx = DummyContext(
        task="Gather X",
        participant_descriptions={"ResearchAgent": "Collect"},
    )
    ledger = DummyLedger(plan_text, facts_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)
    assert mplan.facts == "Known constraints: Budget capped."
    assert len(mplan.steps) == 1
    assert mplan.steps[0].agent == "ResearchAgent"


def test_plan_to_obj_fallback_when_agent_not_in_team():
    plan_text = "- **UnknownAgent** to do something unusual"
    ctx = DummyContext(
        task="Unknown agent test",
        participant_descriptions={"ResearchAgent": "Collect"},
    )
    ledger = DummyLedger(plan_text)
    mgr = _make_manager()

    mplan = mgr.plan_to_obj(ctx, ledger)
    assert len(mplan.steps) == 1
    assert mplan.steps[0].agent == "MagenticAgent"
    assert "do something unusual" in mplan.steps[0].action.lower()
