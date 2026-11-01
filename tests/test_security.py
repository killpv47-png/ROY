import pytest

from cr_analyst.security.crypto import CryptoError, SecretBox, fingerprint, generate_key
from cr_analyst.security.redact import mask, redact, register_secrets


def test_secretbox_fernet_and_passphrase_roundtrip():
    for key in (generate_key(), "a-long-enough-passphrase"):
        box = SecretBox(key)
        tok = box.encrypt("eyJsecret")
        assert tok != "eyJsecret" and box.decrypt(tok) == "eyJsecret"
        assert box.decrypt_bytes(box.encrypt_bytes(b"\x00\x01")) == b"\x00\x01"


def test_secretbox_wrong_key_and_rotation():
    old, new = generate_key(), generate_key()
    tok = SecretBox(old).encrypt("x")
    with pytest.raises(CryptoError):
        SecretBox(new).decrypt(tok)
    assert SecretBox(f"{new},{old}").decrypt(tok) == "x"  # rotation: newest first, old still readable


def test_secretbox_rejects_short_or_missing_key():
    with pytest.raises(CryptoError):
        SecretBox("")
    with pytest.raises(CryptoError):
        SecretBox("short")


def test_fingerprint_stable_and_non_reversible():
    assert fingerprint("abc") == fingerprint("abc") != fingerprint("abd")
    assert "abc" not in fingerprint("abc")


# Synthetic token shapes are assembled at runtime so no token-like literal exists in the source tree.
_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.mark.parametrize("text", [
    "token 123456789:" + "AA" + "HdqTcvCH1vGWJxfSeofSAs0K5PALDsawabcdef",
    "gh" + "p_" + _ALNUM,
    "github" + "_pat_" + "11ABCDEFG0123456789_" + _ALNUM[:26],
    "s" + "k-" + _ALNUM[:26],
    "Authorization: Bearer eyJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJzdXBlcmNlbGwifQ.c2lnbmF0dXJlc2lnbmF0dXJl",
    "postgresql://user:SuperSecret@db.example.com:5432/x",
    "api_key=abcdef123456",
])
def test_redact_patterns(text):
    out = redact(text)
    assert "[REDACTED]" in out
    for secret in ("SuperSecret", _ALNUM, "abcdef123456"):
        if secret in text:
            assert secret not in out


def test_register_custom_secret_and_mask():
    register_secrets(["tvly-custom-secret-value"])
    assert "tvly-custom-secret-value" not in redact("key=tvly-custom-secret-value here")
    assert mask("abcdefghijkl") == "abcd…ijkl"
    assert mask(None) == "—"
