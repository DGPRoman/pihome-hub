"""``pihome-hub-admin`` — the accounts that may log in, and the app phones are offered.

A separate command rather than an HTTP route, for the obvious reason: the first
admin cannot be created through an API that requires an admin. It is also the right
shape for the job — account management is something an operator does once, at a
terminal on the Pi, not something a running service needs to expose.

The ``app`` commands put the Android app's APK where the hub offers it to phones
joining by invitation. They need no database, and so open none.

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
from pihome_hub.android import AndroidApp, NotAnApkError, install_apk
from pihome_hub.config import resolve_android_app_path, resolve_database_path
from pihome_hub.storage import StorageError, prepare_database

PROGRAM: Final = "pihome-hub-admin"

#: Anything the operator can act on. Usage mistakes exit 2, which is argparse's.
EXIT_FAILURE: Final = 1


class AdminError(Exception):
    """Something to report as one line rather than as a traceback."""


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        if args.command == "app":
            target: Path = args.path if args.path is not None else resolve_android_app_path()
            args.run_app(args, target)
        else:
            path: Path = args.database if args.database is not None else resolve_database_path()
            _refuse_to_write_as_the_wrong_user(path)
            prepare_database(path)
            args.run(args, UserStore(path), SessionStore(path))
    except (AdminError, AccountError, StorageError, NotAnApkError, OSError) as exc:
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


_APP_AS_ROOT: Final = "an app installed there as root is one only root can replace"


def _app_install(args: argparse.Namespace, target: Path) -> None:
    _refuse_to_write_as_the_wrong_user(target, _APP_AS_ROOT)
    installed = install_apk(args.file, target)
    _out(f"installed {target} ({installed.size} bytes), sha256 {installed.sha256}")
    _out("phones joining by invitation are offered it from the join page")


def _app_show(args: argparse.Namespace, target: Path) -> None:
    found = AndroidApp(target).describe()
    if found is None:
        _out(f"no app installed at {target}. Install one with: {PROGRAM} app install <file.apk>")
        return
    _out(f"{target} ({found.size} bytes), sha256 {found.sha256}")


def _app_remove(args: argparse.Namespace, target: Path) -> None:
    _refuse_to_write_as_the_wrong_user(target, _APP_AS_ROOT)
    if not target.exists():
        _out(f"no app installed at {target}")
        return
    target.unlink()
    _out(f"removed {target}; the join page no longer offers the app")


# -- Plumbing ----------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Manage the accounts that may log in to this hub, and the Android app it "
            "offers to phones joining by invitation."
        ),
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

    app = commands.add_parser("app", help="the Android app offered to phones joining by invitation")
    app.add_argument(
        "--path",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "where the hub keeps the APK. Defaults to PIHOME_ANDROID_APP_PATH, or to the "
            "state directory the systemd unit declares."
        ),
    )
    app_commands = app.add_subparsers(dest="app_command", required=True)

    install = app_commands.add_parser("install", help="offer this APK, replacing any before it")
    install.add_argument("file", type=Path)
    install.set_defaults(run_app=_app_install)

    show = app_commands.add_parser("show", help="which APK is offered, and its checksum")
    show.set_defaults(run_app=_app_show)

    remove = app_commands.add_parser("remove", help="stop offering the app")
    remove.set_defaults(run_app=_app_remove)

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


def _refuse_to_write_as_the_wrong_user(
    path: Path,
    consequence: str = "a database written there as root is one the service cannot write",
) -> None:
    """Stop root leaving the service a file it will not be able to write.

    On a Pi the state directory belongs to the service account, and a file created
    inside it by root stays owned by root. systemd does not repair that, so the next
    start fails with "attempt to write a readonly database" — a symptom several
    steps removed from its cause. Refusing here names both. An APK installed as root
    would be served, but could then only be replaced as root, so it is refused too.
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

    msg = f"{directory} belongs to {owner!r}, and {consequence}. Run: sudo -u {owner} {PROGRAM} ..."
    raise AdminError(msg)


def _out(line: str) -> None:
    sys.stdout.write(f"{line}\n")


if __name__ == "__main__":
    raise SystemExit(main())
