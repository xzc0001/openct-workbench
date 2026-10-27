"""Import one trusted, self-contained HTML preview via SSH stdin. No HTTP writes."""
import fcntl
import gzip
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
import uuid

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
MAX_BYTES = 512 * 1024 * 1024


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class EmbeddedResources(HTMLParser):
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        resources = []
        if tag in {"script", "img", "source", "video", "audio", "iframe", "embed", "input"}:
            resources.append(attributes.get("src", ""))
        if tag == "object":
            resources.append(attributes.get("data", ""))
        if tag == "link" and "stylesheet" in attributes.get("rel", "").lower():
            resources.append(attributes.get("href", ""))
        if any(value and not value.startswith(("data:", "blob:", "#")) for value in resources):
            raise ValueError("Preview must embed its resources; external or relative assets found")


def rebuild_catalog():
    records = [json.loads(path.read_text(encoding="utf-8")) for path in (ROOT / "records").glob("*.json")]
    records.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    entries = []
    for record in records:
        current = record["current"]
        entries.append({key: record[key] for key in ("id", "title", "summary", "created_at", "updated_at")} | current)
    updated = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    atomic_write(PUBLIC / "catalog.json", (json.dumps({"updated_at": updated, "entries": entries}, ensure_ascii=False, indent=2) + "\n").encode())
    rows = []
    escape = lambda value: html.escape(str(value).replace(" · "," / ").replace("·"," "), quote=True)
    for entry in entries:
        date = datetime.fromisoformat(entry["updated_at"])
        keywords = " ".join([entry["title"], entry["summary"], entry["id"], date.strftime("%Y-%m-%d")]).lower()
        url = escape(entry["url"])
        size = f'{entry["bytes"] / 1048576:.1f}'
        rows.append(f'''<article class="entry" data-search="{escape(keywords)}"><div><h2>{escape(entry["title"])}</h2><p>{escape(entry["summary"])}</p><span class="size">HTML / {size} MB</span></div><div class="date"><time datetime="{escape(entry["updated_at"])}">{date:%Y-%m-%d}</time><br><span>{date:%H:%M} 更新</span></div><div class="actions"><a class="primary" href="{url}">打开预览</a><button type="button" data-share="{url}" aria-label="{escape('分享' + entry['title'])}">复制链接</button><a class="download" href="{url}" download="{escape(entry["id"])}.html">下载离线 HTML</a></div></article>''')
    template = (ROOT / "site" / "index.template.html").read_text(encoding="utf-8")
    page = template.replace("__COUNT__", str(len(entries))).replace("__ENTRIES__", "\n".join(rows)).replace("__UPDATED__", datetime.fromisoformat(updated).strftime("%Y-%m-%d %H:%M"))
    atomic_write(PUBLIC / "index.html", page.encode())
    for name in ("portal.css", "portal.js"):
        atomic_write(PUBLIC / "assets" / name, (ROOT / "site" / name).read_bytes())


def publish(request):
    case_id = request["id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", case_id):
        raise ValueError("Invalid case ID")
    upload = request["upload"]
    if not re.fullmatch(r"[a-f0-9]{32}\.html", upload):
        raise ValueError("Invalid upload filename")
    title = request["title"].strip()
    summary = request.get("summary", "").strip()
    if not title or len(title) > 120 or len(summary) > 500 or any(ord(c) < 32 for c in title + summary):
        raise ValueError("Invalid title or summary")
    source = ROOT / "incoming" / upload
    if source.is_symlink() or not source.is_file() or not 64 <= source.stat().st_size <= MAX_BYTES:
        raise ValueError("Preview must be a regular HTML file between 64 bytes and 512 MiB")
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != request["sha256"].lower():
        raise ValueError("Upload SHA256 mismatch")
    document = payload.decode("utf-8-sig")
    if "<html" not in document[:16384].lower() or "</html>" not in document[-16384:].lower():
        raise ValueError("A complete UTF-8 HTML document is required")
    EmbeddedResources().feed(document)
    case_dir = PUBLIC / "cases" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    target = case_dir / digest
    if not target.exists():
        if shutil.disk_usage(ROOT).free < len(payload) * 2 + 128 * 1024 * 1024:
            raise ValueError("Insufficient free space to publish safely")
        pending = case_dir / (".pending-" + uuid.uuid4().hex)
        pending.mkdir()
        try:
            html_path = pending / "index.html"
            html_path.write_bytes(payload)
            html_path.chmod(0o644)
            with (pending / "index.html.gz").open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as zipped:
                    zipped.write(payload)
            (pending / "index.html.gz").chmod(0o644)
            os.utime(pending / "index.html.gz", ns=(html_path.stat().st_atime_ns, html_path.stat().st_mtime_ns))
            pending.chmod(0o755)
            os.rename(pending, target)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
    elif hashlib.sha256((target / "index.html").read_bytes()).hexdigest() != digest:
        raise ValueError("Existing stored version failed integrity validation")
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    metadata = ROOT / "records" / (case_id + ".json")
    record = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else {"id": case_id, "created_at": now, "versions": []}
    version = {"sha256": digest, "url": f"/cases/{case_id}/{digest}/index.html", "bytes": len(payload)}
    if not any(item["sha256"] == digest for item in record["versions"]):
        record["versions"].append(version | {"published_at": now})
    record.update(title=title, summary=summary, updated_at=now, current=version)
    atomic_write(metadata, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
    rebuild_catalog()
    source.unlink()
    return {"id": case_id, "title": title, **version, "version_count": len(record["versions"])}


if __name__ == "__main__":
    for directory in ("incoming", "records", "public", "public/cases", "public/assets"):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    (ROOT / "incoming").chmod(0o700)
    (ROOT / "records").chmod(0o700)
    try:
        with (ROOT / ".publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if sys.argv[1:] == ["--rebuild"]:
                rebuild_catalog()
                print(json.dumps({"rebuilt": True}))
            else:
                request_text = sys.stdin.read(16385)
                if len(request_text) > 16384:
                    raise ValueError("Publish request too large")
                print(json.dumps(publish(json.loads(request_text)), ensure_ascii=False))
    except (ValueError, KeyError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
