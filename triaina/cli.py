"""Command line interface: list / run / test."""

import argparse
import sys

from .compiler import Program, Diag
from .lexer import LexError
from .parser import ParseError
from . import host


def main(argv=None):
    p = argparse.ArgumentParser(prog='triaina', description='Triaina language tooling')
    sub = p.add_subparsers(dest='cmd', required=True)

    pl = sub.add_parser('list', help='list benches and tests in the import closure')
    pl.add_argument('file')

    pr = sub.add_parser('run', help='run a bench')
    pr.add_argument('file')
    pr.add_argument('--bench', required=True)
    pr.add_argument('--ticks', type=int, default=None, help='run this many ticks')
    pr.add_argument('--until-idle', action='store_true',
                    help='tick until the queue is empty and no events remain')
    pr.add_argument('--max-ticks', type=int, default=100000)
    pr.add_argument('--events', default=None,
                    help='file with lines: TICK KEY VALUE (e.g. "20 t +")')

    pt = sub.add_parser('test', help='run tests in the import closure')
    pt.add_argument('file')
    pt.add_argument('--test', default=None, help='run only this test')

    args = p.parse_args(argv)
    try:
        prog = Program(args.file)
        if args.cmd == 'list':
            print('benches:')
            for b in prog.benches:
                print('  %s' % b.name)
            print('tests:')
            for t in prog.tests:
                print('  %s' % t.name)
            return 0
        if args.cmd == 'run':
            events = host.parse_events_file(args.events) if args.events else None
            rt = host.run_bench(prog, args.bench, ticks=args.ticks,
                                until_idle=args.until_idle, max_ticks=args.max_ticks,
                                events=events)
            for r in rt.records:
                print(r)
            return 0
        if args.cmd == 'test':
            results = host.run_tests(prog, only=args.test)
            if not results:
                print('no tests found')
                return 1
            failed = 0
            for name, ok, msg in results:
                print('%s %s: %s' % ('PASS' if ok else 'FAIL', name, msg))
                if not ok:
                    failed += 1
            return 1 if failed else 0
    except (Diag, ParseError, LexError) as e:
        print('error: %s' % e, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
