from lumen2.actions import Action
from lumen2.kernel import Belief, Kernel, POLICY_CLAMPS
from lumen2.workspace import Workspace


def make_kernel(fixture_copy, name="wrong_operator", **kw):
    ws = Workspace(fixture_copy(name))
    return Kernel("test mission", ws, **kw), ws


def test_belief_updates_move_confidence():
    b = Belief("claim", 0.5)
    b.update(1.0, "support")
    assert b.confidence > 0.5
    b.update(-3.0, "refutation")
    assert b.confidence < 0.5


def test_gate_rejects_test_edit_and_tallies_cheat(fixture_copy):
    kernel, ws = make_kernel(fixture_copy)
    try:
        decision = kernel.gate(Action("edit_file", {
            "path": "test_pricing.py", "content": "def test_x():\n    pass\n"}))
        assert not decision.allowed
        assert kernel.stats.get("cheat:edit-tests") == 1
    finally:
        ws.close()


def test_gate_allows_legit_edit(fixture_copy):
    kernel, ws = make_kernel(fixture_copy)
    try:
        decision = kernel.gate(Action("edit_file", {
            "path": "pricing.py",
            "content": "def apply_discount(price, discount):\n"
                       "    return price - discount\n"}))
        assert decision.allowed
    finally:
        ws.close()


def test_model_critic_can_kill_but_unavailable_is_advisory(fixture_copy):
    edit = Action("edit_file", {"path": "pricing.py",
                                "content": "def apply_discount(p, d):\n"
                                           "    return p - d\n"})
    kernel, ws = make_kernel(fixture_copy,
                             model_critic=lambda p: {"fatal": True,
                                                     "issues": ["bad"]})
    try:
        assert not kernel.gate(edit).allowed
    finally:
        ws.close()
    kernel, ws = make_kernel(fixture_copy, model_critic=lambda p: None)
    try:
        assert kernel.gate(edit).allowed
        assert kernel.stats.get("critic_unavailable") == 1
    finally:
        ws.close()


def test_budgets(fixture_copy):
    kernel, ws = make_kernel(fixture_copy, policy={"max_steps": 2})
    try:
        assert kernel.budget_exceeded() is None
        kernel.step = 2
        assert "step budget" in kernel.budget_exceeded()
        kernel.step = 0
        kernel.tokens_used = 10 ** 9
        assert "token budget" in kernel.budget_exceeded()
    finally:
        ws.close()


def test_policy_revision_is_budgeted_and_clamped(fixture_copy):
    kernel, ws = make_kernel(fixture_copy, policy={"revision_budget": 2,
                                                   "stuck_steps": 4})
    try:
        for _ in range(10):
            kernel.note_progress(False)
            kernel.maybe_revise_policy()
        assert kernel.revisions_used == 2
        lo, hi = POLICY_CLAMPS["temperature"]
        assert lo <= kernel.policy["temperature"] <= hi
        # Budgets are frozen: revision can never touch them.
        assert kernel.policy["max_steps"] == 30
    finally:
        ws.close()


def test_verify_fix_requires_green_and_changes(fixture_copy):
    kernel, ws = make_kernel(fixture_copy)
    try:
        ok, msg = kernel.verify_fix()
        assert not ok  # tests are red and nothing changed
        ws.write_file("pricing.py",
                      '"""Pricing helpers."""\n\n\n'
                      "def apply_discount(price, discount):\n"
                      '    """Return the price after taking the discount off."""\n'
                      "    return price - discount\n")
        ok, msg = kernel.verify_fix()
        assert ok and "verified" in msg
    finally:
        ws.close()
