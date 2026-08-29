"""Small command-family dispatch contract for the authoring CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandFamily:
    commands: frozenset[str]
    handler: Callable[[argparse.Namespace], int]

    def handles(self, command: str) -> bool:
        return command in self.commands


def dispatch_command(
    arguments: argparse.Namespace,
    families: Iterable[CommandFamily],
) -> int | None:
    """Run the one family that owns ``arguments.command``, if migrated."""

    owner = None
    for family in families:
        if family.handles(arguments.command):
            if owner is not None:
                raise ValueError(
                    f"Command {arguments.command!r} belongs to multiple families"
                )
            owner = family
    return None if owner is None else owner.handler(arguments)
