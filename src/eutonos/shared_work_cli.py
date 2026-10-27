"""Configure explicit shared sources and this clone's local participation."""
import argparse
import json
from pathlib import Path
from . import shared_work, tokenslang as ts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    configure = commands.add_parser('configure')
    configure.add_argument('--request', required=True)
    configure.add_argument('--expected-revision', required=True)
    configure.add_argument('--participant')
    mode = configure.add_mutually_exclusive_group()
    mode.add_argument('--enabled', action='store_true')
    mode.add_argument('--disabled', action='store_true')
    fetch = configure.add_mutually_exclusive_group()
    fetch.add_argument('--auto-fetch', action='store_true')
    fetch.add_argument('--no-auto-fetch', action='store_true')
    for child in commands.choices.values(): child.add_argument('--root', default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == 'status': result = {'shared_work': shared_work.load(args.root)}
        else:
            value = json.loads(Path(args.request).read_bytes())
            if args.participant is not None: value['participant'] = args.participant
            if args.enabled or args.disabled: value['enabled'] = args.enabled
            if args.auto_fetch or args.no_auto_fetch: value['auto_fetch'] = args.auto_fetch
            result = shared_work.configure(args.root, value, args.expected_revision)
        print(json.dumps(result, sort_keys=True)); return 0
    except (OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
        print(json.dumps({'outcome': 'error', 'message': str(exc)})); return 2


if __name__ == "__main__":
    raise SystemExit(main())
