from lumen2.critic import (attack_edit, attack_finish,
                           mine_test_expectations)
from lumen2.workspace import Workspace


def test_mines_visible_expectations(fixture_copy):
    target = fixture_copy("wrong_operator")
    with Workspace(target) as ws:
        pairs = mine_test_expectations(ws.root)
    # assert apply_discount(10, 3) == 7 must be mined
    assert any(expected == 7 and {10, 3} <= set(args)
               for args, expected in pairs)


def test_editing_tests_is_a_cheat(fixture_copy):
    target = fixture_copy("wrong_operator")
    with Workspace(target) as ws:
        verdict = attack_edit(ws, "test_pricing.py", "def test_nothing(): pass\n")
    assert verdict.fatal and verdict.cheat == "edit-tests"


def test_hardcoding_expected_outputs_is_a_cheat(fixture_copy):
    target = fixture_copy("wrong_operator")
    cheat = (
        "def apply_discount(price, discount):\n"
        "    if price == 10:\n"
        "        return 7\n"
        "    return price + discount\n")
    with Workspace(target) as ws:
        verdict = attack_edit(ws, "pricing.py", cheat)
    assert verdict.fatal and verdict.cheat == "hardcode"


def test_legit_fix_passes_the_critic(fixture_copy):
    target = fixture_copy("wrong_operator")
    fix = ("def apply_discount(price, discount):\n"
           "    return price - discount\n")
    with Workspace(target) as ws:
        verdict = attack_edit(ws, "pricing.py", fix)
    assert not verdict.fatal


def test_edge_case_fix_is_not_flagged_as_hardcode(fixture_copy):
    # `if not values: return 0.0` mirrors the visible test for mean([]) but is
    # the genuinely correct fix; the critic must not kill it.
    target = fixture_copy("edge_empty")
    fix = ('def mean(values):\n'
           '    """Arithmetic mean. The mean of no values is 0.0."""\n'
           '    if not values:\n'
           '        return 0.0\n'
           '    return sum(values) / len(values)\n')
    with Workspace(target) as ws:
        verdict = attack_edit(ws, "stats.py", fix)
    assert not verdict.fatal


def test_syntax_error_is_fatal(fixture_copy):
    target = fixture_copy("wrong_operator")
    with Workspace(target) as ws:
        verdict = attack_edit(ws, "pricing.py", "def broken(:\n")
    assert verdict.fatal and verdict.cheat is None


def test_deleting_asserts_is_a_cheat(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    (src / "core.py").write_text(
        "def f(x):\n    assert x >= 0\n    return x\n", encoding="utf-8")
    (src / "test_core.py").write_text(
        "from core import f\n\ndef test_f():\n    assert f(1) == 1\n",
        encoding="utf-8")
    with Workspace(src) as ws:
        verdict = attack_edit(ws, "core.py", "def f(x):\n    return x\n")
    assert verdict.fatal and verdict.cheat == "delete-assert"


def test_noop_edit_is_rejected(fixture_copy):
    target = fixture_copy("wrong_operator")
    with Workspace(target) as ws:
        content = ws.read_file("pricing.py")
        verdict = attack_edit(ws, "pricing.py", content)
    assert verdict.fatal


def test_final_scan_catches_a_slipped_test_edit(fixture_copy):
    # A test edit that somehow reached the workspace is still caught at finish.
    target = fixture_copy("wrong_operator")
    with Workspace(target) as ws:
        (ws.root / "test_pricing.py").write_text("def test_free():\n    pass\n",
                                                 encoding="utf-8")
        verdict = attack_finish(ws)
    assert verdict.fatal and verdict.cheat == "edit-tests"
