"""Chained and piped inspection evidence: byte-exact attribution of retained command shapes."""

from pathlib import Path
import shlex
import subprocess  # noqa: F401  (loads the stdlib select module before experiment/src/select.py can shadow it)
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiment/src"))
from common import ReviewError

SOURCE = '''#include "route.h"

bool RouteMgr::updateRoute(const std::string &key)
{
    if (key.find("Vrf") == 0)
    {
        return removeRoute(key);
    }
    return true;
}

bool RouteMgr::removeRoute(const std::string &key)
{
    return m_routes.erase(key) > 0;
}
'''
HEADER = '''#pragma once
class RouteMgr {
public:
    bool updateRoute(const std::string &key);
    bool removeRoute(const std::string &key);
};
'''
CALLER = '''#include "route.h"
void sync(RouteMgr &mgr) { mgr.updateRoute("Vrf1"); }
'''


def numbered(lines, start=1):
    """GNU `nl -ba` / `cat -n` output for already line-ended text."""
    return ''.join(f'{number:>6}\t{line}' for number, line in enumerate(lines, start))


class ChainedInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / 'source'
        (self.repo / 'src').mkdir(parents=True)
        self.source = self.repo / 'src/route.cpp'
        self.source.write_text(SOURCE)
        self.header = self.repo / 'src/route.h'
        self.header.write_text(HEADER)
        self.caller = self.repo / 'src/caller.cpp'
        self.caller.write_text(CALLER)
        (self.repo / 'README.md').write_text('updateRoute is documented here\n')
        self.cards = base / 'prepared/groups/g-1'
        self.cards.mkdir(parents=True)
        self.card = self.cards / 'source-card.md'
        self.card.write_text('# Source card g-1\n\n## src/route.cpp:3-9\n\n```text\n'
                             '     3 | bool RouteMgr::updateRoute(const std::string &key)\n```\n')
        self.scope = self.cards / 'scope.json'
        self.scope.write_text('{"group_id": "g-1", "ranges": [{"start_line": 3, "end_line": 9}]}\n')
        self.references = base / 'references'
        self.references.mkdir()
        (self.references / 'README.md').write_text('Reference: updateRoute semantics\n')
        self.task = {'path': 'src/route.cpp'}
        self.lines = SOURCE.splitlines(keepends=True)
        self.listing = 'caller.cpp\nroute.cpp\nroute.h\n'

    def command(self, identity, script, output, exit_code=0, status='completed', wrapper='/bin/bash -lc '):
        return {"type": "item.completed", "item": {
            "id": identity, "type": "command_execution", "status": status, "exit_code": exit_code,
            "command": (wrapper + shlex.quote(script)) if wrapper else script, "aggregated_output": output}}

    def events(self, *commands):
        return [{"type": "thread.started", "thread_id": "fresh-session"}, {"type": "turn.started"}, *commands,
                {"type": "item.completed", "item": {"type": "agent_message", "text": '{"candidates":[]}'}},
                {"type": "turn.completed", "usage": {"input_tokens": 9, "cached_input_tokens": 0, "output_tokens": 1}}]

    def accept(self, *commands):
        from model_cli import parse_codex
        parse_codex(self.events(*commands), {"candidates": []}, self.task, self.repo)

    def reject(self, *commands):
        from model_cli import parse_codex
        with self.assertRaises(ReviewError):
            parse_codex(self.events(*commands), {"candidates": []}, self.task, self.repo)

    def hits(self, *symbols, relative=False):
        """`rg -n` output for the symbols over src, in a fixed file order."""
        lines = []
        for path in (self.caller, self.header, self.source):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if any(symbol in line for symbol in symbols):
                    lines.append(f'{path.name if relative else path}:{number}:{line}\n')
        return ''.join(lines)

    def header_read(self):
        return self.command('ctx', f'cat {self.header}', HEADER)

    def test_chained_range_reads_and_search_in_one_command_are_credited(self):
        script = (f"sed -n '1,8p' src/route.cpp && sed -n '9,240p' src/route.cpp && "
                  f"rg -n 'updateRoute|removeRoute' src --glob '*.{{cpp,h}}'")
        output = SOURCE + self.hits('updateRoute', 'removeRoute')
        self.accept(self.command('chain', script, output))
        for separator in ('; ', '\n'):
            with self.subTest(separator=separator):
                self.accept(self.command('chain', script.replace(' && ', separator), output))
        self.accept(self.command('chain', script, output, wrapper='/bin/zsh -lc '))
        # A missing range leaves assigned lines uncovered; fabricated bytes earn nothing.
        self.reject(self.command('chain', script, ''.join(self.lines[:8]) + self.hits('updateRoute')))
        self.reject(self.command('chain', script, ''.join(self.lines[:8]) + 'invented\n' + self.hits('updateRoute')))
        # Unrelated search symbols do not provide context even inside an accepted chain.
        self.reject(self.command('chain', script.replace('updateRoute|removeRoute', 'include'),
                                 SOURCE + f'{self.source}:1:#include "route.h"\n'))

    def test_numbered_pipelines_select_single_and_multiple_ranges(self):
        context = self.command('ctx', f"rg -n 'RouteMgr' {self.repo}/src", self.hits('RouteMgr'))
        read = f"nl -ba {self.source} | sed -n '1,6p'; nl -ba {self.source} | sed -n '7,400p'"
        self.accept(self.command('read', read, numbered(self.lines[:6]) + numbered(self.lines[6:], 7)), context)
        # sed prints line 4 twice for overlapping ranges; the exact output is required.
        multi = f"nl -ba {self.source} | sed -n '1,4p;4,10p;11,15p'"
        expected = numbered(self.lines[:4]) + numbered(self.lines[3:10], 4) + numbered(self.lines[10:], 11)
        self.accept(self.command('read', multi, expected), context)
        self.reject(self.command('read', multi, numbered(self.lines)), context)
        self.accept(self.command('read', f"cat -n {self.source} | sed -n '1,15p'", numbered(self.lines)), context)
        self.accept(self.command('read', f"sed -n '1,15p' {self.source} | cat -n", numbered(self.lines)), context)
        self.accept(self.command('read', f"sed -n '9,15p' {self.source} | nl -ba; sed -n '1,8p' {self.source}",
                                 numbered(self.lines[8:]) + ''.join(self.lines[:8])), context)
        self.reject(self.command('read', f"nl -ba {self.source} | sed -n '1,15p'", SOURCE), context)
        self.reject(self.command('read', f"nl -ba {self.source} | sed -n '1,15p'",
                                 numbered(self.lines[:14]) + '    15\t} // end\n'), context)
        self.reject(self.command('read', f"nl -ba {self.source} | sed -n '1,15p'",
                                 numbered(self.lines[:14]) + '    16\t}\n'), context)
        self.reject(self.command('read', f"nl -ba {self.source} | sed -n '1,15p'", numbered(self.lines, 2)), context)

    def test_targeted_habit_card_reads_then_wc_then_numbered_source_read(self):
        card, scope = self.card.read_text(), self.scope.read_text()
        count = SOURCE.count('\n')
        script = f"cat {self.card} {self.scope}; wc -l {self.source}; nl -ba {self.source} | sed -n '1,260p'"
        self.accept(self.command('read', script, card + scope + f'{count} {self.source}\n' + numbered(self.lines)),
                    self.header_read())
        script = (f"sed -n '1,240p' {self.card} && sed -n '1,240p' {self.scope} && "
                  f"wc -l src/route.cpp && sed -n '1,240p' src/route.cpp")
        self.accept(self.command('read', script, card + scope + f'{count} src/route.cpp\n' + SOURCE), self.header_read())
        # The card excerpt and the line count alone never establish coverage.
        self.reject(self.command('read', f"cat {self.card} {self.scope}; wc -l {self.source}",
                                 card + scope + f'{count} {self.source}\n'), self.header_read())
        self.reject(self.command('read', f"cat {self.card}; sed -n '3,3p' {self.source}", card), self.header_read())

    def test_search_roots_may_include_reference_inputs(self):
        read = self.command('read', f'cat {self.source}', SOURCE)
        script = f"rg -n 'updateRoute' {self.repo}/src {self.references}"
        reference_hit = f'{self.references}/README.md:1:Reference: updateRoute semantics\n'
        self.accept(read, self.command('ctx', script, self.hits('updateRoute') + reference_hit))
        self.accept(read, self.command('ctx', script, reference_hit + self.hits('updateRoute')))
        # Hits outside every root, reference-only hits, and tool diagnostics alone are not context.
        self.reject(read, self.command('ctx', script, self.hits('updateRoute')
                                       + f'{self.temp.name}/elsewhere.cpp:1:updateRoute();\n'))
        self.reject(read, self.command('ctx', script, reference_hit))
        self.reject(read, self.command('ctx', f"rg -n 'updateRoute' {self.references}", reference_hit))
        self.reject(read, self.command('ctx', script, 'rg: src: No such file or directory (os error 2)\n'))

    def test_context_lines_and_bounded_searches_are_verified(self):
        read = self.command('read', f'cat {self.source}', SOURCE)
        context = ('3:bool RouteMgr::updateRoute(const std::string &key)\n4-{\n'
                   '5-    if (key.find("Vrf") == 0)\n')
        self.accept(read, self.command('ctx', f"rg -n -A 2 'updateRoute' {self.source}", context))
        self.reject(read, self.command('ctx', f"rg -n -A 2 'updateRoute' {self.source}", context.replace('4-{', '4-}')))
        multi = (f'{self.caller}:2:void sync(RouteMgr &mgr) {{ mgr.updateRoute("Vrf1"); }}\n--\n'
                 f'{self.header}-3-public:\n{self.header}:4:    bool updateRoute(const std::string &key);\n')
        self.accept(read, self.command('ctx', f"rg -n -C 1 updateRoute {self.repo}/src | head -40", multi))
        self.accept(read, self.command('ctx', f"grep -rn updateRoute {self.repo}/src | head -n 40", self.hits('updateRoute')))
        self.accept(read, self.command('ctx', f"rg -n --glob '*.{{cpp,h}}' updateRoute {self.repo}/src 2>/dev/null | head -80",
                                       self.hits('updateRoute')))
        self.reject(read, self.command('ctx', f"rg -n updateRoute {self.repo}/src > /tmp/hits.txt", self.hits('updateRoute')))
        self.reject(read, self.command('ctx', f"rg -n updateRoute {self.repo}/src | tee /tmp/hits.txt", self.hits('updateRoute')))

    def test_multi_file_cat_credits_each_source_file(self):
        self.accept(self.command('read', f'cat {self.source} {self.header}', SOURCE + HEADER))
        self.accept(self.command('read', f'cat {self.header} {self.source}', HEADER + SOURCE))
        self.reject(self.command('read', f'cat {self.source} {self.header}', SOURCE + 'invented\n'))
        self.accept(self.command('read', f"sed -n '1,15p' {self.source}", SOURCE),
                    self.command('ctx', f'cat {self.header} {self.caller}', HEADER + CALLER))

    def test_failed_trailing_search_keeps_verified_reads_in_a_chain(self):
        script = f"sed -n '1,15p' {self.source}; rg -n 'updateRoute' src -g '*.{{h,cpp}}'"
        output = SOURCE + 'rg: src: No such file or directory (os error 2)\n'
        self.accept(self.command('read', script, output, exit_code=2, status='failed'), self.header_read())
        self.reject(self.command('read', script, output, exit_code=2, status='failed'))
        # A single failed command is still not evidence.
        self.reject(self.command('read', f'cat {self.source}', SOURCE, exit_code=1, status='failed'), self.header_read())
        self.reject(self.command('read', f'cat {self.source}', SOURCE, exit_code=0, status='failed'), self.header_read())

    def test_pending_searches_before_a_read_are_credited_together(self):
        script = (f"rg -n 'updateRoute' {self.repo}/src | head -240\nrg -n 'removeRoute' {self.repo}/src 2>/dev/null\n"
                  f"nl -ba {self.source} | sed -n '1,100p'")
        output = self.hits('updateRoute') + self.hits('removeRoute') + numbered(self.lines)
        self.accept(self.command('chain', script, output))
        self.reject(self.command('chain', script, self.hits('updateRoute') + 'fabricated:1:updateRoute\n'
                                 + numbered(self.lines)))
        # An opaque listing between the searches and the read leaves the searches unattributed.
        listing = f"rg -n 'updateRoute' {self.repo}/src\nls {self.repo}/src\nnl -ba {self.source} | sed -n '1,100p'"
        self.reject(self.command('chain', listing, self.hits('updateRoute') + self.listing + numbered(self.lines)))

    def test_opaque_commands_and_echoed_bytes_never_count(self):
        self.reject(self.command('chain', f'ls {self.repo}/src; echo {shlex.quote(SOURCE)}', self.listing + SOURCE),
                    self.header_read())
        self.reject(self.command('chain', f'ls {self.repo}/src; printf %s {shlex.quote(SOURCE)}', self.listing + SOURCE),
                    self.header_read())
        self.reject(self.command('read', f'cat {self.card}; wc -l {self.source}', self.card.read_text() + f'15 {self.source}\n'),
                    self.header_read())
        self.reject(self.command('read', f"awk 'NR<=15' {self.source}", SOURCE), self.header_read())

    def test_cd_changes_resolution_for_later_commands(self):
        script = f"cd {self.repo}/src && sed -n '1,15p' route.cpp && rg -n updateRoute ."
        self.accept(self.command('chain', script, SOURCE + self.hits('updateRoute', relative=True)))
        self.accept(self.command('chain', f"cd {self.repo} && sed -n '1,15p' src/route.cpp; cat src/route.h", SOURCE + HEADER))
        self.reject(self.command('chain', f"cd {self.references} && sed -n '1,15p' route.cpp", SOURCE), self.header_read())
        self.reject(self.command('chain', f"cd /tmp && cat {self.source.name}", SOURCE), self.header_read())

    def test_head_and_tail_forms_cover_ranges(self):
        context = self.header_read()
        self.accept(self.command('read', f'head -n 8 {self.source}; tail -n +9 {self.source}', SOURCE), context)
        self.accept(self.command('read', f'head -8 {self.source} && tail -7 {self.source}', SOURCE), context)
        self.accept(self.command('read', f'cat {self.source} | head -n 8; cat {self.source} | tail -n 7', SOURCE), context)
        self.reject(self.command('read', f'head -n 8 {self.source}; tail -n 6 {self.source}',
                                 ''.join(self.lines[:8]) + ''.join(self.lines[9:])), context)
        self.reject(self.command('read', f'head -n 8 {self.source}; tail -n 7 {self.source}',
                                 ''.join(self.lines[:8]) + ''.join(self.lines[9:])), context)

    def test_truncated_chain_segment_stops_further_credit(self):
        script = f"sed -n '1,8p' {self.source} && sed -n '9,15p' {self.source} && cat {self.header}"
        self.reject(self.command('chain', script, ''.join(self.lines[:8]) + ''.join(self.lines[8:14]) + HEADER))
        self.accept(self.command('chain', script, SOURCE + HEADER))


if __name__ == "__main__":
    unittest.main()
