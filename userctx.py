"""
userctx.py -- "who is this code running for?"

Every request and every background job runs on behalf of exactly one signed-in
user. Two things follow from that user, and both are looked up through here so
the rest of the code doesn't have to be passed a user everywhere:

  * which database file to use  (db.path() asks for it)
  * which API keys / email credentials to use  (config.env() asks for them)

It uses contextvars, so concurrent requests and job threads never see each
other's user. Outside any user context (tests, one-off scripts) both lookups
return None and the app falls back to the single-user behaviour.
"""
import contextvars
from contextlib import contextmanager

_user = contextvars.ContextVar("hub_user", default=None)
_db = contextvars.ContextVar("hub_db_path", default=None)


def set_context(user, db_path):
    """Returns tokens for reset_context()."""
    return (_user.set(user), _db.set(db_path))


def reset_context(tokens):
    _user.reset(tokens[0])
    _db.reset(tokens[1])


def get_user():
    return _user.get()


def db_path():
    return _db.get()


@contextmanager
def use(user, db_path):
    tokens = set_context(user, db_path)
    try:
        yield
    finally:
        reset_context(tokens)
