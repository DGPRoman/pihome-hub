"""``pihome-hub-admin`` — creating and managing the accounts that may log in.

A separate command rather than an HTTP route, for the obvious reason: the first
admin cannot be created through an API that requires an admin. It is also the right
shape for the job — account management is something an operator does once, at a
terminal on the Pi, not something a running service needs to expose.

Nothing here takes a password as an argument. A command line is visible in ``ps``
to every account on the machine and is written to shell history, and a password
that has reached either is a password to change.
"""

from __future__ import annotations

import argparse
import getpass
import os
import pwd
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pihome_hub.accounts import AccountError, Role, SessionStore, UserStore, check_username
from pihome_hub.config import resolve_database_path
from pihome_hub.storage import StorageError, prepare_database

PROGRAM: Final = "pihome-hub-admin"

#: Anything the operator can act on. Usage mistakes exit 2, which is argparse's.
EXIT_FAILURE: Final = 1


class AdminError(Exception):
    """Something to report as one line rather than as a traceback."""


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path: Path = args.database if args.database is not None else resolve_database_path()

    try:
        _refuse_to_write_as_the_wrong_user(path)
        prepare_database(path)
        args.run(args, UserStore(path), SessionStore(path))
    except (AdminError, AccountError, StorageError) as exc:
        sys.stderr.write(f"{PROGRAM}: {exc}\n")
        return EXIT_FAILURE
    except (KeyboardInterrupt, EOFError):
        # Ctrl-C or Ctrl-D at a password prompt is someone changing their mind, not
        # a fault worth printing a stack for.
        sys.stderr.write("\ncancelled\n")
        return EXIT_FAILURE

    return 0


# -- Commands ----------------------------------------------------------------


def _create(args: argparse.Namespace, store: UserStore, sessions: SessionStore) -> None:
    # Checked before the prompt rather than left to the store, which checks it too.
    # Not duplicated logic — the same function, called early, so that nobody types a
    # password twice only to be told the name was never going to be accepted.
    check_username(args.username)

    user = store.create(args.username, _read_password(), Role(args.role))
    _out(f"created {user.username!r} as {user.role.value}")


def _list(args: argparse.Namespace, store: UserStore, sessions: SessionStore) -> None:
    users = store.list_users()
    if not users:
        _out(f"no accounts yet. Create one with: {PROGRAM} create <username> --role admin")
        return

    width = max(len("USERNAME"), *(len(user.username) for user in users))
    _out(f"{'USERNAME':<{width}}  {'ROLE':<8}  {'STATE':<8}  CREATED")
    for user in users:
        state = "disabled" if user.disabled else "enabled"
        created = f"{user.created_at:%Y-%m-%d %H:%M}"
        _out(f"{user.username:<{width}}  {user.role.value:<8}  {state:<8}  {created}")


def _passwd(args: argparse.Namespace, store: UserStore, sessions: SessionStore) -> None:
    # Looked up before the prompt for the same reason, and for one more: the store
    # hashes before it reads, so a short password for a name that does not exist was
    # reported as a weak password. The operator's actual mistake was the name.
    user = store.get(args.username)

    store.set_password(user.username, _read_password())
    # A new password that left the old sessions running would not lock anybody out,
    # which is most of the reason to change one. Unlike disabling or deleting — both
    # of which a session notices by itself, because resolving one re-reads the
    # account — this needs saying, since the row a session points at has not changed.
    ended = sessions.destroy_all_for(user)

    closed = "" if ended == 0 else f", and {ended} open session(s) closed"
    _out(f"password changed for {user.username!r}{closed}")


def _role(args: argparse.Namespace, store: UserStore, sessions: SessionStore) -> None:
    user = store.set_role(args.username, Role(args.role))
    _out(f"{user.username!r} is now {user.role.value}")


def _disable(args: argparse.Namespace, store: UserStore, sessions: SessionStore) -> None:
    user = store.set_disabled(args.username, True)
    _out(f"{user.username!r} disabled. Its sessions stop working at once; the password is kept")


def _enable(args: argparse.Namespace, store: UserStore, sessions: SessionStore) -> None:
    user = store.set_disabled(args.username, False)
    _out(f"{user.username!r} enabled")


def _delete(args: argparse.Namespace, store: UserStore, sessions: SessionStore) -> None:
    # Confirmed only where there is someone to ask. Scripted, --yes is not required:
    # a script that reached this line already said what it meant.
    if not args.yes and sys.stdin.isatty():
        answer = input(f"Delete account {args.username!r}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            _out("cancelled")
            return

    store.delete(args.username)
    _out(f"{args.username!r} deleted")


# -- Plumbing ----------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Manage the accounts that may log in to this hub.",
        epilog=(
            "Passwords are read from the terminal, or from stdin when there is none, "
            "and never taken as arguments."
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "the accounts database. Defaults to PIHOME_DATABASE_PATH, or to the state "
            "directory the systemd unit declares."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    roles = [role.value for role in Role]

    create = commands.add_parser("create", help="add an account")
    create.add_argument("username")
    create.add_argument("--role", choices=roles, required=True)
    create.set_defaults(run=_create)

    listing = commands.add_parser("list", help="show every account")
    listing.set_defaults(run=_list)

    passwd = commands.add_parser("passwd", help="change an account's password")
    passwd.add_argument("username")
    passwd.set_defaults(run=_passwd)

    role = commands.add_parser("role", help="change what an account may do")
    role.add_argument("username")
    role.add_argument("role", choices=roles)
    role.set_defaults(run=_role)

    disable = commands.add_parser("disable", help="block an account without deleting it")
    disable.add_argument("username")
    disable.set_defaults(run=_disable)

    enable = commands.add_parser("enable", help="let a disabled account log in again")
    enable.add_argument("username")
    enable.set_defaults(run=_enable)

    delete = commands.add_parser("delete", help="remove an account")
    delete.add_argument("username")
    delete.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    delete.set_defaults(run=_delete)

    return parser


def _read_password() -> str:
    """From the terminal, or from a pipe when there is no terminal."""
    if not sys.stdin.isatty():
        # Scripted use: one line on stdin, and nobody to ask for a confirmation.
        return sys.stdin.readline().rstrip("\n")

    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        msg = "the two passwords did not match"
        raise AdminError(msg)
    return first


def _refuse_to_write_as_the_wrong_user(path: Path) -> None:
    """Stop root leaving the service a database it will not be able to write.

    On a Pi the state directory belongs to the service account, and a file created
    inside it by root stays owned by root. systemd does not repair that, so the next
    start fails with "attempt to write a readonly database" — a symptom several
    steps removed from its cause. Refusing here names both.
    """
    directory = path.parent
    if os.geteuid() != 0 or not directory.exists():
        return

    owner_id = directory.stat().st_uid
    if owner_id == 0:
        return

    try:
        owner = pwd.getpwuid(owner_id).pw_name
    except KeyError:
        owner = str(owner_id)

    msg = (
        f"{directory} belongs to {owner!r}, and a database written there as root is one "
        f"the service cannot write. Run: sudo -u {owner} {PROGRAM} ..."
    )
    raise AdminError(msg)


def _out(line: str) -> None:
    sys.stdout.write(f"{line}\n")


if __name__ == "__main__":
    raise SystemExit(main())
