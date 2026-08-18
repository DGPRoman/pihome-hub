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

What it buys is release automation. Versions had sat at `0.1.0` with no tags,
no releases and no changelog; `.github/workflows/release.yml` now derives all
three from the commit types.
