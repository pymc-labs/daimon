"""Sensitivity probes edit loaded production code, never production files."""

from __future__ import annotations
import __future__

import ast
import importlib
import inspect
import textwrap
from types import CodeType, FunctionType
from typing import Any, cast

import pytest


def replace_constant(code: CodeType, before: str, after: str) -> CodeType:
    return code.replace(
        co_consts=tuple(
            replace_constant(value, before, after)
            if isinstance(value, CodeType)
            else after
            if value == before
            else value
            for value in code.co_consts
        )
    )


class LedgerDate(ast.NodeTransformer):
    def visit_keyword(self, node: ast.keyword) -> ast.keyword:
        if node.arg == "occurred_at":
            assert isinstance(node.value, ast.Attribute) and node.value.attr == "processed_at"
            node.value = ast.Constant(None)
        return node


class DmDelivery(ast.NodeTransformer):
    def visit_Subscript(self, node: ast.Subscript) -> ast.expr:
        if isinstance(node.value, ast.Name) and node.value.id == "answer":
            return ast.copy_location(ast.Constant("mutated private delivery"), node)
        return node


def apply(patch: pytest.MonkeyPatch, mutation: str) -> None:
    if mutation == "slack_eyes":
        module: Any = importlib.import_module("daimon.adapters.slack.app")
        function = cast(FunctionType, module.SlackApp._orchestrate)
        code = replace_constant(function.__code__, "eyes", "hourglass")
    elif mutation == "discord_eyes":
        module = importlib.import_module("daimon.adapters.discord.lifecycle")
        function = cast(FunctionType, module.DiscordTurnLifecycle.on_acknowledgment)
        code = replace_constant(function.__code__, "👀", "👁")
    else:
        if mutation == "ledger_dating":
            module = importlib.import_module("daimon.core.usage_recording")
            function = cast(FunctionType, module.record_turn_usage)
            transformer = LedgerDate()
        elif mutation == "dm_delivery":
            module = importlib.import_module("daimon.adapters.discord.commands.direct_messages")
            function = cast(FunctionType, module.DirectMessageCog.on_message)
            transformer = DmDelivery()
        else:
            raise AssertionError(f"Unknown oracle mutation: {mutation}")
        source = ast.parse(textwrap.dedent(inspect.getsource(function)))
        definition = source.body[0]
        assert isinstance(definition, ast.AsyncFunctionDef)
        definition.decorator_list = []
        mutated = ast.fix_missing_locations(transformer.visit(source))
        namespace = dict(function.__globals__)
        exec(
            compile(
                mutated,
                function.__code__.co_filename,
                "exec",
                flags=__future__.annotations.compiler_flag,
            ),
            namespace,
        )
        code = cast(FunctionType, namespace[function.__name__]).__code__
    assert (
        code.co_code != function.__code__.co_code or code.co_consts != function.__code__.co_consts
    ), f"Mutation did not modify production code: {mutation}"
    patch.setattr(function, "__code__", code)
