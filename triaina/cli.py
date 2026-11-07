"""Command line interface: list / run / test / repl."""

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

    pe = sub.add_parser('repl', help='minimal interactive terminal host for a bench')
    pe.add_argument('file')
    pe.add_argument('--bench', required=True)
    pe.add_argument('--pace', type=float, default=0.0,
                    help='minimum seconds between tick starts when pacing')

    args = p.parse_args(argv)
    try:
        prog = Program(args.file)
        if args.cmd == 'list':
            print('benches:')
            for _fi, b in prog.benches:
                print('  %s' % ('::'.join(b.qname)))
            print('tests:')
            for _fi, t in prog.tests:
                print('  %s' % ('::'.join(t.qname)))
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
        if args.cmd == 'repl':
            return _repl(prog, args)
    except (Diag, ParseError, LexError) as e:
        print('error: %s' % e, file=sys.stderr)
        return 2


def _repl(prog, args):
    rt, comp = host.build_bench_rt(prog, args.bench)
    print('bench %r ready; tick %d (init snapshot observed); commands: step, '
          'advance N, tick, outputs, records, diags, pace S, start, pause, stop, '
          '<input record e.g. t+>, quit' % (args.bench, rt.tick))
    for r in rt.records:
        print(r)
    while True:
        try:
            line = input('tri> ')
        except EOFError:
            break
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0]
        try:
            if cmd in ('quit', 'q', 'exit'):
                break
            elif cmd in ('step', 's'):
                rt.step()
            elif cmd in ('advance', 'a') and len(parts) == 2:
                rt.advance(int(parts[1]))
            elif cmd == 'tick':
                print('completed tick: %d' % rt.tick)
                continue
            elif cmd in ('outputs', 'o'):
                for label, val in rt.outputs().items():
                    print('%s: %s' % (label, val))
                continue
            elif cmd == 'records':
                for r in rt.records:
                    print(r)
                continue
            elif cmd == 'diags':
                ds = rt.diagnostics()
                print('\n'.join(ds) if ds else '(no diagnostics)')
                continue
            elif cmd == 'pace' and len(parts) == 2:
                args.pace = float(parts[1])
                print('pace interval: %ss' % args.pace)
                continue
            elif cmd == 'start':
                if not hasattr(args, '_pacer') or args._pacer is None:
                    from .runtime import Pacer
                    args._pacer = Pacer(rt, args.pace)
                args._pacer.interval = args.pace
                args._pacer.start()
                args._pacer.resume()
                print('paced execution started (tick %d)' % rt.tick)
                continue
            elif cmd == 'pause':
                if getattr(args, '_pacer', None):
                    args._pacer.pause()
                    print('paused at tick %d' % rt.tick)
                continue
            elif cmd == 'stop':
                if getattr(args, '_pacer', None):
                    args._pacer.stop()
                    args._pacer = None
                    print('stopped at tick %d' % rt.tick)
                continue
            else:
                key, trits = host.parse_keyboard_record(line, rt)
                tick = rt.inject(key, trits)
                print('input %r = %s assigned to tick %d'
                      % (key, ''.join(trits), tick))
                continue
        except (Diag, ValueError, KeyError) as e:
            print('error: %s' % e)
            continue
        for r in rt.records[-5:]:
            if r.tick >= rt.tick - 1:
                pass
        print('tick %d' % rt.tick)
        for label, val in rt.outputs().items():
            print('%s: %s' % (label, val))
    if getattr(args, '_pacer', None):
        args._pacer.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
