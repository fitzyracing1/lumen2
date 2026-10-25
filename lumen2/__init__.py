"""Lumen 2 — a bounded kernel wrapped around a multi-step, tool-using agent.

The model proposes one tool action at a time. The kernel gates every action:
a critic attacks edits and final answers, verification runs before any commit,
confidence is explicit, budgets are hard, and withholding is a first-class
outcome.
"""

__version__ = "2.0.0"
