"""Product/date export names with exclusive, temporary collision reservations."""
from contextlib import contextmanager
from datetime import datetime
from itertools import count
from pathlib import Path
import os
import re


def output_directory(directory):
    """Reject blank/relative destinations even for older direct callers."""
    if not isinstance(directory, (str, Path)) or not str(directory).strip():
        raise ValueError('请选择成片输出文件夹')
    folder = Path(directory)
    if not folder.is_absolute():
        raise ValueError('请选择绝对路径的成片输出文件夹')
    return folder


@contextmanager
def reserve_output(directory, product_name):
    folder = output_directory(directory)
    folder.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(product_name or '').strip()[:5]) or '商品'
    now = datetime.now()
    stem = f'{prefix} {now.month}.{now.day}.{now.hour}'
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]', stem.split('.', 1)[0].rstrip(), re.IGNORECASE):
        stem = '_' + stem
    for number in count(1):
        suffix = '' if number == 1 else f' ({number})'
        output = folder / f'{stem}{suffix}.mp4'
        if os.path.lexists(output):
            continue
        marker = output.with_name(output.name + '.reserve')
        try:
            reservation = marker.open('x')
        except FileExistsError:
            continue
        try:
            with reservation:
                # Another producer may have published before we acquired the
                # reservation. Existing video bytes always win.
                if os.path.lexists(output):
                    continue
                yield output
                return
        finally:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                # A locked marker is retained; future exports skip that name.
                # Cleanup must not discard an already published video.
                pass
