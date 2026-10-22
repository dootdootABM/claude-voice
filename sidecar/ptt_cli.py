"""`cv ptt down|up|toggle|status`: press the push-to-talk key of the running app from anywhere (a window-manager
binding, AutoHotkey, a script). Standard library only, so it starts fast. Exit status: 0 done, 1 not running, 2 usage."""

import sys

import platform_util
import ptt

USAGE = "usage: cv ptt down|up|toggle|status"


def main(argv=None):
    platform_util.utf8_console()
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1 or args[0].lower() not in ptt.COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        reply = ptt.send_command(args[0].lower())
    except ConnectionError as e:
        print(f"cv ptt: {e}", file=sys.stderr)
        return 1
    print(reply)
    return 1 if reply.startswith("error") else 0


if __name__ == "__main__":
    sys.exit(main())
