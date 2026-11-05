import secrets as _test_secrets
_TEST_PASSWORD_0 = _test_secrets.token_urlsafe(24)
"""Authenticated disposable clients for HTTP regression tests."""
from auth import Accounts, COOKIE
import urllib.request


def fixture(root):
    store=Accounts(root/'test-auth.sqlite3')
    if not store.users():store.create('tester',_TEST_PASSWORD_0,'admin')
    token=store.login('tester',_TEST_PASSWORD_0,'test')
    cookie=COOKIE+'='+token
    principal=store.session(cookie)
    headers={'Cookie':cookie,'X-CSRF-Token':principal['csrf']}
    return store,headers


def opener(headers):
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    op.addheaders=list(headers.items())
    return op
