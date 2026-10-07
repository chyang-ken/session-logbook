"""Every option on the Agent CLI explains itself.

This CLI is read almost entirely by Agents, and `--help` is the only description they get
at the moment they need it. An option whose help is empty is one the caller has to guess
at or learn by experiment -- the same gap that made turn slicing unreachable before it
existed. A check here is cheaper than noticing later, one option at a time.
"""

import argparse
import unittest

import session_logbook_cli as cli


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.items()


class HelpCoverageTests(unittest.TestCase):
    def test_every_subcommand_says_what_it_does(self):
        parser = cli.build_parser()
        commands = dict(_subparsers(parser))
        self.assertTrue(commands, "the CLI exposes no subcommands")
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for choice in action.choices:
                    listed = [item for item in action._choices_actions if item.dest == choice]
                    self.assertTrue(listed and listed[0].help,
                                    f"subcommand {choice!r} has no help")

    def test_every_option_explains_itself(self):
        bare = []
        for name, sub in _subparsers(cli.build_parser()):
            for action in sub._actions:
                if not action.option_strings or isinstance(action, argparse._HelpAction):
                    continue
                if not (action.help or "").strip():
                    bare.append(f"{name} {action.option_strings[0]}")
        self.assertEqual(bare, [], "these options carry no help: " + ", ".join(bare))

    def test_every_positional_explains_itself(self):
        bare = []
        for name, sub in _subparsers(cli.build_parser()):
            for action in sub._actions:
                if action.option_strings or isinstance(action, argparse._SubParsersAction):
                    continue
                if not (action.help or "").strip():
                    bare.append(f"{name} <{action.dest}>")
        self.assertEqual(bare, [], "these arguments carry no help: " + ", ".join(bare))

    def test_parsing_still_goes_through_the_same_parser(self):
        args = cli.parse_args(["context", "target", "--last-turns", "2"])
        self.assertEqual((args.command, args.target, args.last_turns), ("context", "target", 2))


if __name__ == "__main__":
    unittest.main()
