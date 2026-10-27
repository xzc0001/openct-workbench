"""Authenticated disposable clients for HTTP regression tests."""
from auth import Accounts, COOKIE
import urllib.request
import secrets

TEST_PASSWORD = secrets.token_urlsafe(24)


def fixture(root):
    store=Accounts(root/'test-auth.sqlite3')
    if not store.users():store.create('tester',TEST_PASSWORD,'admin')
    token=store.login('tester',TEST_PASSWORD,'test')
    cookie=COOKIE+'='+token
    principal=store.session(cookie)
    headers={'Cookie':cookie,'X-CSRF-Token':principal['csrf']}
    return store,headers


def opener(headers):
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    op.addheaders=list(headers.items())
    return op
