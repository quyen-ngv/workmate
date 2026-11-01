"""The command line builds and every team command parses (a syntax error in a CLI module must fail the suite)."""

from __future__ import annotations

import pytest

from actoroffice.__main__ import build_parser


@pytest.mark.parametrize(
    "argv",
    [
        ["init"],
        ["user", "add", "Lan", "--roles", "ba", "--telegram", "111"],
        ["team", "add", "Shop", "--jira", "SHP"],
        ["team", "member", "Shop", "Lan", "ba"],
        ["project", "add", "shop", "--path", "."],
        ["project", "set", "shop", "--team", "Shop", "--profile", "{}"],
        ["task", "Thử", "--team", "Shop", "--owner", "Lan", "--project", "shop", "--template", "code"],
        ["task", "Thử", "--model", "opus"],
        ["model", "AO-1"],
        ["model", "AO-1", "opus", "--when", "stage"],
        ["model", "AO-1", "haiku", "--stage", "plan", "--when", "set"],
        ["model", "AO-1", "--cancel"],
        ["stage", "AO-1", "steer", "ưu", "tiên", "VIP"],
        ["rules", "add", "Test Java", "--body", "JUnit 5", "--scope", "global"],
        ["resource", "add", "payments", "--kind", "http", "--config", "{}"],
        ["jira", "sync"],
        ["bitbucket", "a@b.c", "tok", "ws"],
        ["doctor"],
        ["serve", "--no-browser"],
    ],
)
def test_commands_parse(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    assert callable(args.fn)
