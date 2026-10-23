"""Source-backed native inspection evidence; never execute saved commands."""

import os
from pathlib import Path
import re
import shlex

from common import EXTENSIONS, ReviewError, require, within


KEYWORDS = set('auto bool break case char class const continue default delete else false float for '
               'include int namespace nullptr private public return sizeof static std struct switch '
               'template this true unsigned using virtual void while'.split())


def command_argv(command, repo):
    if not isinstance(command, str):
        return [], repo
    try:
        argv = shlex.split(command)
        if len(argv) == 3 and Path(argv[0]).name in ('sh', 'bash', 'zsh') and argv[1] in ('-c', '-lc'):
            command = argv[2]
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split = True
        argv = list(lexer)
        cwd = repo
        if len(argv) >= 4 and argv[0] == 'cd' and argv[2] == '&&':
            cwd = source_path(repo, repo, argv[1])
            if not cwd.is_dir():
                return [], repo
            argv = argv[3:]
        # A bounded search still provides context. Coverage is checked against
        # actual retained bytes, so truncation cannot imply a whole-file read.
        if '|' in argv:
            index = argv.index('|')
            suffix = argv[index + 1:]
            if not (len(suffix) == 3 and suffix[:2] == ['head', '-n'] and suffix[2].isdigit()):
                return [], cwd
            argv = argv[:index]
        if any(token in (';', '&&', '||', '|', '>', '<', '>>', '&') for token in argv):
            return [], cwd
        return argv, cwd
    except (ValueError, ReviewError):
        return [], repo


def source_path(repo, cwd, value):
    candidate = Path(os.path.abspath(cwd / value))
    require(candidate.is_relative_to(repo), 'inspection path is outside source')
    return repo if candidate == repo else within(repo, candidate.relative_to(repo).as_posix())


def read_evidence(argv, output, repo, cwd):
    """Return the source path and exactly observed one-based lines, or None."""
    if len(argv) < 2:
        return None
    tool, args = Path(argv[0]).name, argv[1:]
    start, end, numbered = 1, None, False
    if tool == 'cat':
        numbered = '-n' in args or '--number' in args
        args = [a for a in args if a not in ('--', '-n', '--number')]
    elif tool == 'nl':
        if args[:1] == ['-ba']:
            args = args[1:]
        elif args[:2] == ['-b', 'a']:
            args = args[2:]
        else:
            return None
        numbered = True
    elif tool == 'sed':
        if args[:1] != ['-n']:
            return None
        args = args[1:]
        if args[:1] == ['-e']:
            args = args[1:]
        match = re.fullmatch(r'([1-9][0-9]*)(?:,([1-9][0-9]*|\$))?p', args[0]) if args else None
        if not match:
            return None
        start = int(match[1])
        end = None if match[2] == '$' else int(match[2] or match[1])
        args = args[1:]
    elif tool in ('head', 'tail'):
        if len(args) < 3 or args[0] != '-n' or not args[1].lstrip('+').isdigit():
            return None
        count = int(args[1])
        if tool == 'head':
            end = count
        elif args[1].startswith('+'):
            start = count
        else:
            start = -count
        args = args[2:]
    else:
        return None
    args = [a for a in args if a != '--']
    if len(args) != 1:
        return None
    path = source_path(repo, cwd, args[0])
    if not path.is_file():
        return None
    lines = path.read_bytes().decode('utf-8').splitlines(keepends=True)
    if start < 0:
        start = max(1, len(lines) + start + 1)
    end = min(end if end is not None else len(lines), len(lines))
    expected = ''.join(lines[start - 1:end])
    if numbered:
        observed = []
        for index, line in enumerate(output.splitlines(keepends=True), start):
            match = re.fullmatch(r'\s*([0-9]+)\t(.*)', line, re.DOTALL)
            if not match or int(match[1]) != index:
                return None
            observed.append(match[2])
        output = ''.join(observed)
    if output != expected or not output:
        return None
    return path, set(range(start, end + 1)), output


def search_evidence(argv, output, repo, cwd, identifiers):
    if not argv or Path(argv[0]).name not in ('rg', 'grep'):
        return False
    args, positional, pattern = argv[1:], [], None
    numbered = False
    literal = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--':
            positional.extend(args[i + 1:])
            break
        if arg in ('-e', '--regexp'):
            i += 1
            if i == len(args): return False
            pattern = args[i]
        elif arg in ('-g', '--glob', '--include', '-A', '-B', '-C', '--context', '--max-count', '-m'):
            i += 1
            if i == len(args): return False
        elif arg.startswith('--'):
            if arg not in ('--line-number', '--fixed-strings', '--with-filename', '--no-heading',
                           '--hidden', '--recursive'):
                return False
            numbered |= arg == '--line-number'
            literal |= arg == '--fixed-strings'
        elif arg.startswith('-'):
            if not set(arg[1:]) <= set('nFrRHEwiS'):
                return False
            numbered |= 'n' in arg
            literal |= 'F' in arg
        else:
            positional.append(arg)
        i += 1
    if pattern is None:
        if not positional: return False
        pattern = positional.pop(0)
    if not numbered or not positional:
        return False
    symbol_pattern = pattern if literal else pattern.replace(r'\b', '').replace(r'\<', '').replace(r'\>', '')
    symbols = identifiers & set(re.findall(r'[A-Za-z_][A-Za-z_0-9]{2,}', symbol_pattern))
    if not symbols:
        return False
    roots = [source_path(repo, cwd, value) for value in positional]
    if not all(path.exists() for path in roots):
        return False
    source_match = False
    for line in output.splitlines():
        match = re.fullmatch(r'(.+?):([1-9][0-9]*):(.*)', line)
        if match:
            value, number, content = match.groups()
            path = source_path(repo, cwd, value)
        elif len(roots) == 1 and roots[0].is_file():
            match = re.fullmatch(r'([1-9][0-9]*):(.*)', line)
            if not match: return False
            number, content = match.groups()
            path = roots[0]
        else:
            return False
        if not path.is_file() or not any(path == root or path.is_relative_to(root) for root in roots):
            return False
        actual = path.read_bytes().decode('utf-8').splitlines()
        if int(number) > len(actual) or actual[int(number) - 1] != content:
            return False
        if literal and pattern not in content:
            return False
        source_match |= path.suffix in EXTENSIONS and any(symbol in content for symbol in symbols)
    return source_match


def validate_inspection(events, task, repo):
    repo = Path(os.path.abspath(repo))
    source = within(repo, task['path'])
    text = source.read_bytes().decode('utf-8')
    identifiers = set(re.findall(r'\b[A-Za-z_][A-Za-z_0-9]{2,}\b', text)) - KEYWORDS
    covered, read_ids, context_ids, ids = set(), set(), set(), set()
    final_index = max(i for i, event in enumerate(events) if event.get('type') == 'item.completed'
                      and isinstance(event.get('item'), dict) and event['item'].get('type') == 'agent_message')
    for event in events[:final_index]:
        item = event.get('item')
        if event.get('type') != 'item.completed' or not isinstance(item, dict) or item.get('type') != 'command_execution':
            continue
        identity = item.get('id')
        require(isinstance(identity, str) and identity and identity not in ids,
                'missing or duplicate inspection command ID')
        ids.add(identity)
        if item.get('status') != 'completed' or type(item.get('exit_code')) is not int or item['exit_code'] != 0:
            continue
        output = item.get('aggregated_output')
        if not isinstance(output, str) or not output.strip():
            continue
        argv, cwd = command_argv(item.get('command'), repo)
        try:
            read = read_evidence(argv, output, repo, cwd)
            if read:
                path, lines, content = read
                if path == source:
                    covered.update(lines)
                    read_ids.add(identity)
                elif path.suffix in EXTENSIONS and identifiers & set(re.findall(r'\b\w+\b', content)):
                    context_ids.add(identity)
            if search_evidence(argv, output, repo, cwd, identifiers):
                context_ids.add(identity)
        except (ReviewError, OSError, UnicodeError):
            continue
    require(covered == set(range(1, len(text.splitlines()) + 1)) and read_ids and context_ids
            and len(read_ids | context_ids) >= 2,
            'incomplete source inspection evidence: require assigned-file coverage and related source context')
