"""비밀번호 해시 (DB 없이)."""

from __future__ import annotations

import pytest

from services.auth import hash_password, token_digest, verify_password


def test_hash_is_salted_and_verifies():
    a, b = hash_password("correct horse"), hash_password("correct horse")
    assert a != b                      # 소금이 달라 같은 비밀번호도 다른 해시
    assert a.startswith("scrypt$")
    assert "correct horse" not in a    # 원문이 남지 않는다
    assert verify_password("correct horse", a)
    assert verify_password("correct horse", b)


@pytest.mark.parametrize("stored", [None, "", "plain-text", "bcrypt$x$y", "scrypt$1$2$3$!!$??"])
def test_bad_or_missing_hash_never_verifies(stored):
    assert verify_password("anything", stored) is False


def test_wrong_password_rejected():
    assert verify_password("wrong", hash_password("right-password")) is False


def test_empty_password_cannot_be_hashed():
    with pytest.raises(ValueError):
        hash_password("")


def test_token_digest_is_sha256_hex():
    d = token_digest("abc")
    assert len(d) == 64 and int(d, 16) >= 0
    assert d != "abc"
