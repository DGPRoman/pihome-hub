# Contributing

## Commit messages

Subjects follow [Conventional Commits](https://www.conventionalcommits.org):

```
type(optional-scope)!: description
```

Bodies do not change. This repository has always explained *why* a change is
right rather than restating the diff, and that is the part worth keeping — the
subject line simply gains a prefix that a tool can read.

| Part | Rule |
| --- | --- |
| `type` | one of `build` `chore` `ci` `docs` `feat` `fix` `perf` `refactor` `revert` `style` `test` |
| `scope` | optional, lower case: `relays` `sensors` `automation` `accounts` `api` `storage` `admin` `config` `security` `ratelimit` `deploy` `docs` `tests` `ci` |
| `!` | append to the type or scope for a breaking change, and explain it in a `BREAKING CHANGE:` footer |
| `description` | lower case, imperative, no trailing full stop, whole subject within 72 characters |
| body | separated by one blank line, wrapped at 72, present for anything not self-evident |

Footers, where they apply: `Fixes: #123`, `Refs: #123`, `BREAKING CHANGE: ...`.

```
fix(relays): resolve preserve without biasing the pin it reads

The probe opened the pin with pull_up=False, which does not leave the pull
alone: gpiozero assigns a pull-down and then measures the line it just
pulled. On an undriven pin that reads low, and active_low turns low into
logical on — so the relay closed a mains circuit at startup on the strength
of a level the probe itself created.

Fixes: #22
```

### Enable the hook

Once per clone:

```bash
git config core.hooksPath .githooks
git config commit.template .gitmessage
```

`.githooks/commit-msg` rejects a message that does not fit. It is a plain
POSIX shell script rather than a linter from a package registry, because this
repository has no Node toolchain and because CI runs that same file over every
commit in a pull request — one definition of valid, in one place.

### Why this changed

The history before this point uses capitalised, prefix-free subjects in Git's
own style. Those commits are left alone: rewriting them would change every
hash and break the links from issues and pull requests that refer to them. The
log therefore has a visible seam, which is the honest cost of the change.

The convention was adopted for release automation, and that automation has since
been removed. release-please can only open its pull request if the repository
allows GitHub Actions to create and approve pull requests — a permission that
also lets a workflow approve one, which is wider than the automation was worth on
a repository whose pull requests are reviewed by hand anyway.

What the convention still buys is a log that says what each change *is* rather
than only what it touched, and a history a tool can read. Tags and a changelog
can be derived from it later, or the automation restored behind a token of its
own, without rewriting anything a second time.

## Dependencies

Direct versions are declared in `pyproject.toml`. Everything actually installed —
transitive packages included — is pinned with hashes in `requirements/`, one file per
install shape:

| File | Contents | Used by |
| --- | --- | --- |
| `base.txt` | Runtime only | `deploy/install.sh` on a host with no GPIO |
| `rpi.txt` | Runtime and the hardware backend | `deploy/install.sh` on a Pi |
| `dev.txt` | Runtime and the tooling | CI, and a development checkout |
| `dev-rpi.txt` | Runtime, the tooling and the hardware backend | The CI leg that exercises `relays/gpio.py` |

After changing a dependency in `pyproject.toml`, regenerate them:

```sh
requirements/refresh.sh
```

That keeps every version already pinned and moves only what the change forces, so
editing one dependency does not drag the whole tree forward. To move the tree on
purpose — picking up upstream fixes — pass the flag through:

```sh
requirements/refresh.sh --upgrade
```

CI runs the same script and fails if the result differs from what is committed, which
is what stops the lockfiles from quietly ceasing to describe the project.
