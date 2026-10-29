"""dev-team-gate Hermes plugin: one `pre_tool_call` hook, see guard.py.

Stdlib only. Hermes is imported lazily by board.KanbanBoard, inside the hook.
"""
from .guard import pre_tool_call


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)
