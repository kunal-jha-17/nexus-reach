"""
cryptobox.py -- the one place that knows the server's SECRET_KEY.

It signs login cookies (via Flask), encrypts each user's saved API keys / email
passwords, and encrypts the copies of the databases sent to outside storage.

Rotating the key without losing anything:
    1. SECRET_KEY_OLD=<the current key>      (comma-separate several old ones)
       SECRET_KEY=<a new random key>
    2. python3 manage.py rotate-key           (re-encrypts everyone's saved keys)
    3. once that has run and the next sync finished, delete SECRET_KEY_OLD
New data is always written with SECRET_KEY; anything written with an old key
can still be read while it's listed in SECRET_KEY_OLD.
"""
import base64
import gzip
import hashlib
import os
import secrets as pysecrets
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

import config


class KeyError_(RuntimeError):
    pass


@lru_cache(maxsize=8)
def _file_key(folder):
    f = Path(folder) / "secret_key"
    if f.exists():
        return f.read_text().strip()
    key = pysecrets.token_hex(32)
    f.write_text(key)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return key


def keys():
    """[current, *old] as bytes. The current key is first."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    old = [k.strip() for k in os.environ.get("SECRET_KEY_OLD", "").split(",") if k.strip()]
    if env_key:
        return [env_key.encode()] + [k.encode() for k in old if k != env_key]
    if config.sync_enabled():
        # A key file would sit on a disk that's wiped on restart -- and everyone's saved
        # keys would become unreadable each time. Better to refuse to start.
        raise KeyError_("SECRET_KEY is not set. When the app copies its data to outside storage you must set "
                        "SECRET_KEY yourself (any long random string) in the host's environment settings, "
                        "otherwise every restart would lose everyone's saved keys.")
    return [_file_key(str(config.data_dir())).encode()]


def secret_key():
    return keys()[0]


def fallback_keys():
    return keys()[1:]


def _fernet_for(k):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(k + b"|user-credentials").digest()))


def fernet():
    return MultiFernet([_fernet_for(k) for k in keys()])


def encrypt_text(text):
    return fernet().encrypt(text.encode()).decode()


def decrypt_text(token):
    """Returns the text, or None if no known key can read it."""
    try:
        return fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def encrypt_blob(data):
    """gzip then encrypt (used for database copies sent to outside storage)."""
    return fernet().encrypt(gzip.compress(data, 6))


def decrypt_blob(token):
    try:
        return gzip.decompress(fernet().decrypt(token))
    except (InvalidToken, OSError, ValueError) as e:
        raise KeyError_("Couldn't decrypt the stored copy -- is SECRET_KEY (or SECRET_KEY_OLD) the key it was "
                        "saved with?") from e


def is_primary(token):
    """True if `token` was encrypted with the current key (False = needs re-encrypting)."""
    try:
        Fernet(base64.urlsafe_b64encode(hashlib.sha256(keys()[0] + b"|user-credentials").digest())).decrypt(token.encode())
        return True
    except (InvalidToken, ValueError):
        return False
