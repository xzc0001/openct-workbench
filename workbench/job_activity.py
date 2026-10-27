"""Read-only evidence of work, including carriage-return nnU-Net tile progress."""
from pathlib import Path
import os
import re
import time

BLOCK = re.compile(r'\d{1,3}%\|[^\r\n]*?\|\s*(\d+)\s*/\s*(\d+)\s*\[')


def model_blocks(text):
    result = None
    for line in re.split(r'[\r\n]', text):
        if any(marker in line for marker in ('Resampling...', 'Saving segmentations...', 'Generating rough segmentation', 'Predicting...')):
            result = None
        match = BLOCK.search(line)
        if match:
            current, total = map(int, match.groups())
            if total > 0 and 0 <= current <= total:
                result = {'current': current, 'total': total}
    return result


def process_usage(pid, proc=Path('/proc')):
    pending, seen, ticks, waiting = [pid], set(), 0, False
    while pending:
        current = pending.pop()
        if current in seen:continue
        seen.add(current)
        try:
            fields = (proc/str(current)/'stat').read_text().rsplit(') ', 1)[1].split()
            ticks += sum(int(fields[n]) for n in (11, 12, 13, 14))
            waiting |= fields[0] == 'D'
            pending.extend(map(int, (proc/str(current)/'task'/str(current)/'children').read_text().split()))
        except (OSError, IndexError, ValueError):
            if current == pid:return None
    return {'cpu_seconds': ticks / os.sysconf('SC_CLK_TCK'), 'io_waiting': waiting}


def activity(job, status, progress, process, previous=None, now=None):
    now = time.time() if now is None else now
    result = {'elapsed_seconds': max(0, int(now-status.get('started_at', status['updated_at']))), 'process_alive': process.poll() is None}
    usage = process_usage(process.pid) if os.name != 'nt' else None
    if usage:
        result.update(usage)
        if previous and 'cpu_seconds' in previous:
            result['cpu_active'] = usage['cpu_seconds']-previous['cpu_seconds'] > .05
    if progress.get('stage') == 'segmentation':
        try:
            with (job/'worker.log').open('rb') as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, progress.get('log_start_bytes', 0), size-65536))
                blocks = model_blocks(stream.read(65536).decode('utf-8', errors='replace'))
            if blocks:result['blocks'] = blocks
        except OSError:pass
    return result
