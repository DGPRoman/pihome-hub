"""Guards on what the repository itself must not come to contain.

Not about the code's behaviour but about the checkout: a rule that keeps a runtime
artefact out of a commit is only as good as the agreement between where the code
writes and what git ignores, and nothing else in the suite would notice those two
drifting apart.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pihome_hub.config import resolve_database_path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Resolved once. The suite runs where git may not be installed — a container built
#: from the wheel, for one — and an absent binary is a reason to skip rather than fail.
_GIT = shutil.which("git")


class TestTheDevelopmentDatabaseIsIgnored:
    """Off systemd the database lands inside the checkout, where git can see it.

    ``docs/devices.md`` states that each announced key is stored in the clear, because
    a hash cannot be replayed on the next poll; sessions are in the same file. A
    development run leaves all of that at ``var/hub.db``, one ``git add -A`` away from
    a public repository. The path is derived here rather than written down, so moving
    the fallback fails this test instead of quietly moving the file out from under the
    rule that covers it.
    """

    @pytest.fixture
    def default_path(self, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.delenv("STATE_DIRECTORY", raising=False)
        monkeypatch.delenv("PIHOME_DATABASE_PATH", raising=False)
        return resolve_database_path()

    def test_the_fallback_is_inside_the_checkout(self, default_path: Path) -> None:
        # The premise of the test below. If the fallback ever becomes absolute —
        # /var/lib, a temporary directory — git is no longer what keeps it out and
        # this class should go rather than be made to pass.
        assert not default_path.is_absolute()

    @pytest.mark.skipif(
        _GIT is None or not (REPO_ROOT / ".git").exists(),
        reason="not a git checkout with git available, so there is no ignore rule to consult",
    )
    def test_git_ignores_it(self, default_path: Path) -> None:
        # check-ignore rather than a read of .gitignore: the question is what git
        # concludes, including negations and any other ignore file it consults.
        result = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
            [str(_GIT), "check-ignore", "--quiet", "--no-index", str(default_path)],
            cwd=REPO_ROOT,
            check=False,
        )

        assert result.returncode == 0, f"{default_path} is not ignored by git"
