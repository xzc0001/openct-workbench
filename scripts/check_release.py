"""Check the allowlisted working tree, Git index, or every reachable Git object.

An additional release gate, not a clinical-data anonymizer or secret-detection
guarantee. Findings never print matched values. Run a separate secret scanner
and review comments/templates manually before publishing.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
MANIFEST='release-files.json'
ALLOWED_DOMAINS={
    'github.com','threejs.org','slicer.readthedocs.io','www.slicer.org',
    'pytorch.org','download.pytorch.org','simpleitk.org','pydicom.github.io',
    'nipy.org','vtk.org','numpy.org','scipy.org','scikit-image.org',
    'python-pillow.org','www.radix-ui.com','nginx.org','www.python.org',
    'nodejs.org','systemd.io','www.w3.org','developer.mozilla.org','discourse.threejs.org',
}
FORBIDDEN_PARTS={'jobs','cache','exports','auth','run','weights','runtime',
                 'records','public','incoming','revisions','node_modules','__pycache__'}
ALLOWED_SUFFIXES={'.py','.js','.cjs','.css','.html','.md','.txt','.json','.yml','.yaml'}
RULES={
    'credential-token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})'),
    'private-key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'credential-in-url':re.compile(r'https?://[^\s/]+:[^\s/]+@'),
    'absolute-user-path':re.compile(r'[A-Za-z]:[\\/](?:Users)[\\/]|' + '/ho' + r'me/[^\s/]+/'),
    'institution-specific-name':re.compile(r'[\u4e00-\u9fff]{2,}(?:医院|卫生院|卫生中心)|[A-Za-z]+ Hospital'),
    'inline-image-payload':re.compile(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/]{100,}'),
}

def git(*args):
    return subprocess.check_output(['git','-C',str(ROOT),*args],stderr=subprocess.DEVNULL)

def check(name,data,allowed,issues,metadata=False):
    def issue(kind):issues.append({'file':name,'rule':kind})
    if not metadata:
        if name not in allowed:issue('not-in-release-allowlist')
        path=PurePosixPath(name)
        if FORBIDDEN_PARTS.intersection(path.parts):issue('runtime-data-path')
        if path.suffix not in ALLOWED_SUFFIXES and name not in {'.gitignore','LICENSE'}:issue('unexpected-file-type')
        if path.name=='config.json':issue('local-config')
    try:text=data.decode('utf-8')
    except UnicodeDecodeError:issue('non-text-file');return
    if '\x00' in text:issue('binary-content')
    for kind,pattern in RULES.items():
        if pattern.search(text):issue(kind)
    for match in re.finditer(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])',text):
        try:address=ipaddress.ip_address(match.group())
        except ValueError:continue
        if not address.is_loopback and str(address) not in {'0.0.0.0'}:issue('non-loopback-ipv4')
    for match in re.finditer(r'https?://[^\s<>"\x27`\)]+',text):
        try:host=urlsplit(match.group()).hostname
        except ValueError:issue('malformed-url');continue
        if not host:continue
        if host in ALLOWED_DOMAINS or host in {'127.0.0.1','localhost'}:continue
        if host.endswith(('.invalid','.example')):continue
        issue('unreviewed-external-domain')
    # Templates must not contain a serialized patient volume/model payload.
    if name.endswith('.html'):
        for match in re.finditer(r'<script[^>]+id="(?:ctData|sceneData|volumeRoiData)"[^>]*>(.*?)</script>',text,re.S):
            if not re.fullmatch(r'\s*__[A-Z_]+__\s*',match.group(1)):issue('embedded-study-data')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--staged',action='store_true')
    parser.add_argument('--history',action='store_true')
    args=parser.parse_args()
    allowed=set(json.loads((ROOT/MANIFEST).read_text(encoding='utf-8')))
    issues=[];checks=0
    if args.history:
        for line in git('rev-list','--objects','--all').decode().splitlines():
            oid,_,name=line.partition(' ')
            kind=git('cat-file','-t',oid).strip()
            if kind==b'blob':
                check(name,git('cat-file','blob',oid),allowed,issues);checks+=1
            elif kind==b'commit':
                check('commit-metadata',git('cat-file','commit',oid),allowed,issues,True)
    elif args.staged:
        for record in git('ls-files','--stage','-z').decode().split('\0'):
            if not record:continue
            info,name=record.split('\t',1);mode,oid,stage=info.split()
            if mode not in {'100644','100755'}:issues.append({'file':name,'rule':'non-regular-git-entry'})
            check(name,git('cat-file','blob',oid),allowed,issues);checks+=1
    else:
        for name in sorted(allowed):
            path=ROOT/name
            if not path.is_file() or path.is_symlink():issues.append({'file':name,'rule':'missing-or-symlink'});continue
            check(name,path.read_bytes(),allowed,issues);checks+=1
        # With Git initialized, also refuse any extra nonignored file.
        try:
            names=git('ls-files','--cached','--others','--exclude-standard','-z').decode().split('\0')
            for name in names:
                if name and name not in allowed:issues.append({'file':name,'rule':'extra-file'})
        except subprocess.CalledProcessError:pass
    print(json.dumps({'checked_files':checks,'findings':issues,'passed':not issues},ensure_ascii=False,indent=2))
    return bool(issues)

if __name__=='__main__':sys.exit(main())
