"""Configure, submit, inspect and recover the single scoped Git publisher."""
import argparse
import json
from pathlib import Path

from .git_delivery import DeliveryError, Engine, thaw
from . import git_delivery_native as native, tokenslang as ts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest='operation', required=True)
    configure = sub.add_parser('configure')
    configure.add_argument('--input', type=Path, required=True)
    configure.add_argument('--expected-revision', type=int, default=0)
    for operation in ('submit', 'submit-code'):
        submit = sub.add_parser(operation)
        submit.add_argument('--input', type=Path, required=True, help='id/session/work_id and exact changes with base64 before/after')
    for operation in ('run', 'resume', 'recover'):
        selected = sub.add_parser(operation)
        selected.add_argument('request_id')
        if operation == 'recover':
            selected.add_argument('--host', type=Path)
            selected.add_argument('--resume', action='store_true', help='Explicitly authorize same-binding paused request at current configuration revision')
    inspect = sub.add_parser('inspect'); inspect.add_argument('request_id', nargs='?')
    sub.add_parser('status'); sub.add_parser('drain-verified')
    # Both root placements are accepted by Settings forwarding and direct CLI.
    for child in sub.choices.values():
        child.add_argument('--root', type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        from .private_scope import require_shared
        require_shared(args.root)
        if args.operation in {'status', 'inspect'}:
            result = native.inspect(args.root, getattr(args, 'request_id', None))
        else:
            engine = Engine(args.root)
            if args.operation == 'configure':
                result = engine.configure(json.loads(args.input.read_text(encoding='utf-8')), expected_revision=args.expected_revision)
            elif args.operation in {'submit', 'submit-code'}:
                value = json.loads(args.input.read_text(encoding='utf-8'))
                key = engine.submit_code(value['id'], {p: {k: thaw(v) for k, v in c.items()} for p, c in value['changes'].items()},
                                         session=value['session'], work_id=value['work_id'])
                result = {'request_id': key, **engine.run(key)}
            elif args.operation == 'run':
                result = engine.run(args.request_id)
            elif args.operation == 'resume':
                result = engine.resume(args.request_id)
            elif args.operation == 'recover':
                snapshot = native.inspect(args.root, args.request_id)
                if snapshot['native_intents'] and not snapshot['requests']:
                    if not args.host:
                        raise DeliveryError('Native intent recovery requires the original --host.')
                    result = native.recover_request(engine, args.request_id, args.host)
                else:
                    if args.resume:
                        engine.resume(args.request_id)
                    result = engine.run(args.request_id)
            else:
                result = {'requests': engine.drain_verified()}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1 if isinstance(result, dict) and result.get('state') in {'blocked', 'paused', 'unavailable', 'waiting-native', 'push-uncertain', 'commit-uncertain'} else 0
    except (ts.ReadError, DeliveryError, ValueError, OSError, KeyError, __import__('sqlite3').Error) as exc:
        print(json.dumps({'outcome': 'attention-required', 'message': str(exc)})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
