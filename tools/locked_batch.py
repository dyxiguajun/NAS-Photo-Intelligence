"""Execute one job holding a non-blocking file lock for the process lifetime."""
import argparse
import fcntl
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lock',type=Path)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if not command:
        parser.error('A batch command is required')
    fd = os.open(args.lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print(f'Active job holds {args.lock}',file=sys.stderr)
        raise SystemExit(75)
    # The kernel releases the lock when the command and descendants close it.
    # Exec preserves the file descriptor without an independent lock process.
    os.set_inheritable(fd,True)
    os.execvp(command[0],command)


if __name__ == '__main__':
    main()
