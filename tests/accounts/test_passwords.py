"""Password hashing: what a stored hash promises, and what it refuses."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from collections.abc import Callable

import pytest

from pihome_hub.accounts import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    InvalidPasswordHashError,
    WeakPasswordError,
    dummy_verify,
    hash_password,
    needs_rehash,
    passwords,
    verify_password,
)

PASSWORD = "correct-horse-battery-staple"


class TestHashAndVerify:
    def test_a_password_verifies_against_its_own_hash(self) -> None:
        assert verify_password(PASSWORD, hash_password(PASSWORD))

    def test_a_wrong_password_does_not(self) -> None:
        assert not verify_password("something-else-entirely", hash_password(PASSWORD))

    def test_one_character_off_does_not(self) -> None:
        assert not verify_password(PASSWORD + "!", hash_password(PASSWORD))

    def test_the_same_password_hashes_differently_every_time(self) -> None:
        """A salt per hash. Without one, equal passwords are visible as equal rows."""
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)

        assert first != second
        assert verify_password(PASSWORD, first)
        assert verify_password(PASSWORD, second)

    def test_the_hash_does_not_contain_the_password(self) -> None:
        assert PASSWORD not in hash_password(PASSWORD)

    def test_it_records_the_parameters_it_used(self) -> None:
        """Self-describing, so the work factor can be raised without a flag day."""
        assert hash_password(PASSWORD).startswith("scrypt$n=16384,r=8,p=1$")


class TestNormalisation:
    def test_the_same_password_typed_two_ways_is_one_password(self) -> None:
        """Composed and decomposed accents look identical and are different bytes.

        NFC, per RFC 8265. Which form arrives depends on the keyboard and the
        platform, so without this an account is reachable from one machine only.
        """
        # Written as escapes: the two are indistinguishable on screen, and a test
        # whose point is that they differ should not rely on the reader seeing it.
        composed = "wachtwoord-caf\u00e9"
        decomposed = "wachtwoord-cafe\u0301"

        assert composed != decomposed, "same bytes would make this prove nothing"
        assert verify_password(decomposed, hash_password(composed))


class TestStrength:
    def test_a_short_password_is_refused_before_it_is_hashed(self) -> None:
        with pytest.raises(WeakPasswordError, match=f"at least {MIN_PASSWORD_LENGTH} characters"):
            hash_password("x" * (MIN_PASSWORD_LENGTH - 1))

    def test_the_minimum_itself_is_accepted(self) -> None:
        assert verify_password("x" * MIN_PASSWORD_LENGTH, hash_password("x" * MIN_PASSWORD_LENGTH))

    def test_an_empty_password_is_refused(self) -> None:
        with pytest.raises(WeakPasswordError):
            hash_password("")

    def test_the_message_does_not_repeat_the_password(self) -> None:
        """It is printed to a terminal and may end up in a journal."""
        secret = "hunter2"

        with pytest.raises(WeakPasswordError) as raised:
            hash_password(secret)

        assert secret not in str(raised.value)

    def test_an_absurdly_long_password_is_refused(self) -> None:
        with pytest.raises(WeakPasswordError, match=f"at most {MAX_PASSWORD_LENGTH}"):
            hash_password("x" * (MAX_PASSWORD_LENGTH + 1))

    def test_verifying_an_over_long_password_answers_no_without_hashing_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Avoiding the work is the point, so "it returns False" is not the claim.

        Without the second half this passes with the guard deleted: an over-long
        password is the wrong password either way.
        """
        encoded = hash_password(PASSWORD)

        def refuse(*args: object, **kwargs: object) -> bytes:
            raise AssertionError("the over-long password was hashed after all")

        monkeypatch.setattr(hashlib, "scrypt", refuse)

        assert not verify_password("x" * (MAX_PASSWORD_LENGTH + 1), encoded)

    def test_a_password_shorter_than_the_current_minimum_still_verifies(self) -> None:
        """Raising the minimum must not lock out the account that needs to change it."""
        short = "x" * (MIN_PASSWORD_LENGTH - 1)

        assert verify_password(short, _hash_with(short))


class TestOlderParameters:
    def test_it_verifies_with_the_parameters_the_hash_records(self) -> None:
        """The property that makes raising the work factor survivable."""
        assert verify_password(PASSWORD, _hash_with(PASSWORD, n=1024))

    def test_a_fresh_hash_does_not_need_rehashing(self) -> None:
        assert not needs_rehash(hash_password(PASSWORD))

    def test_a_weaker_hash_does(self) -> None:
        assert needs_rehash(_hash_with(PASSWORD, n=1024))

    def test_a_shorter_salt_does_too(self) -> None:
        """Not only the work factor: a hash is as good as its weakest recorded field."""
        encoded = _hash_with(PASSWORD, salt=b"01234567")

        assert verify_password(PASSWORD, encoded), "it must still be readable to be upgraded"
        assert needs_rehash(encoded)


class TestMalformedHashes:
    """A stored hash that is not one of ours is a storage fault, not a wrong password."""

    #: Each case damages exactly one thing about a hash that is otherwise real. Written
    #: as edits rather than as literals on purpose: hand-written literals had a
    #: too-short salt as well as the defect under test, so every case was refused for
    #: the same reason and the list proved one guard nine times.
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda fields: [], id="no fields at all"),
            pytest.param(lambda fields: fields[:3], id="truncated"),
            pytest.param(lambda fields: [*fields, "extra"], id="a field too many"),
            pytest.param(lambda fields: ["bcrypt", *fields[1:]], id="another algorithm"),
            pytest.param(
                lambda fields: [fields[0], "n=16384,r=8", *fields[2:]], id="a missing parameter"
            ),
            pytest.param(
                lambda fields: [fields[0], "n=16384,r=8,p=1,q=9", *fields[2:]],
                id="a parameter we do not know",
            ),
            pytest.param(
                lambda fields: [fields[0], "n=lots,r=8,p=1", *fields[2:]],
                id="a parameter that is not a number",
            ),
            pytest.param(
                lambda fields: [fields[0], "n,r=8,p=1", *fields[2:]],
                id="a parameter with no value",
            ),
            # str.isdigit() is true for these and int() parses them to 16384, so
            # without the ascii check the format would have two spellings of one hash.
            pytest.param(
                lambda fields: [fields[0], "n=١٦٣٨٤,r=8,p=1", *fields[2:]],
                id="a parameter in another script's digits",
            ),
            pytest.param(
                lambda fields: [*fields[:2], "not base64!", fields[3]],
                id="a salt that is not base64",
            ),
        ],
    )
    def test_it_is_refused(self, damage: Callable[[list[str]], list[str]]) -> None:
        fields = hash_password(PASSWORD).split("$")
        assert verify_password(PASSWORD, "$".join(fields)), "the undamaged hash must verify"

        with pytest.raises(InvalidPasswordHashError):
            verify_password(PASSWORD, "$".join(damage(fields)))

    def test_a_truncated_key_is_refused_rather_than_matched(self) -> None:
        """A one-byte key would be guessed in 256 tries, and we never wrote one."""
        good = hash_password(PASSWORD)
        head, key = good.rsplit("$", 1)

        with pytest.raises(InvalidPasswordHashError, match="expected at least"):
            verify_password(PASSWORD, f"{head}${base64.b64encode(b'x').decode('ascii')}")

        assert verify_password(PASSWORD, f"{head}${key}"), "the unedited hash should still work"

    def test_a_hash_demanding_more_memory_than_the_ceiling_is_refused(self) -> None:
        """Otherwise an edited row turns a login into a several-gigabyte allocation."""
        # Edited by field rather than by str.replace: the base64 either side is not
        # ours to make assumptions about.
        fields = hash_password(PASSWORD).split("$")
        fields[1] = "n=1073741824,r=8,p=1"

        with pytest.raises(InvalidPasswordHashError, match="more memory"):
            verify_password(PASSWORD, "$".join(fields))

    def test_needs_rehash_refuses_them_too(self) -> None:
        """Answering True would send a corrupt row down the path that overwrites it."""
        with pytest.raises(InvalidPasswordHashError):
            needs_rehash("not a hash at all")


class TestDummyVerify:
    def test_it_answers_no(self) -> None:
        assert not dummy_verify(PASSWORD)

    def test_it_costs_what_a_real_verification_costs(self) -> None:
        """Timing it would be flaky; the parameters it runs at are the real claim.

        If the dummy hash were left behind at older parameters, "no such user" would
        again answer faster than "wrong password" and the endpoint would be an oracle
        for which usernames exist.
        """
        assert not needs_rehash(passwords._dummy_hash())


class TestConcurrencyIsBounded:
    def test_only_a_few_hashes_run_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each holds 16 MiB, on a board with 512 MB for the whole system.

        Unbounded, a burst of logins is uvicorn's forty threadpool workers at once
        and the kernel picks which process dies.
        """
        lock = threading.Lock()
        live = 0
        peak = 0

        def slow_scrypt(*args: object, **kwargs: object) -> bytes:
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)
            with lock:
                live -= 1
            return bytes(32)

        monkeypatch.setattr(hashlib, "scrypt", slow_scrypt)
        threads = [threading.Thread(target=hash_password, args=(PASSWORD,)) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert peak > 1, "twelve threads never overlapped, so this measured nothing"
        # A literal, not passwords._MAX_CONCURRENT: a bound read from the thing under
        # test moves with it, and the assertion holds however far the limit is raised.
        assert peak <= 4


def _hash_with(password: str, *, n: int = 16384, salt: bytes = b"0123456789abcdef") -> str:
    """An encoded hash built here rather than by the module under test.

    Lets a test state what an *older* build would have stored, which the module has
    no way to produce any more.
    """
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=8, p=1, dklen=32, maxmem=64 * 1024**2
    )
    return "$".join(
        (
            "scrypt",
            f"n={n},r=8,p=1",
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(key).decode("ascii"),
        )
    )
