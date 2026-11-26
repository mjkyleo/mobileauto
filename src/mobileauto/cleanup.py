"""运行数据清理：给调试图片和采集批次设置上限，避免长期运行无限积累。"""

import os
import shutil
import time


def prune_files(path, max_files=60, max_age_days=3, patterns=None):
    """清理目录内旧文件（不递归）。返回删除数量。max_age_days=0 表示不按天清理。"""
    if not os.path.isdir(path):
        return 0
    if patterns is None:
        patterns = (".png", ".jpg", ".jpeg", ".txt", ".xml")
    entries = []
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        if patterns and not name.lower().endswith(tuple(patterns)):
            continue
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        entries.append((mtime, full))
    entries.sort(key=lambda item: item[0], reverse=True)

    removed = 0
    if max_age_days and max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        stale = [(t, p) for t, p in entries if t < cutoff]
        for _, full in stale:
            try:
                os.remove(full)
                removed += 1
            except OSError:
                pass
        entries = [(t, p) for t, p in entries if t >= cutoff]

    if max_files and max_files > 0:
        for _, full in entries[max_files:]:
            try:
                os.remove(full)
                removed += 1
            except OSError:
                pass
    return removed


def prune_dirs(path, max_dirs=8, max_age_days=30):
    """清理采集批次目录，保留最近若干批。返回删除数量。"""
    if not os.path.isdir(path):
        return 0
    entries = []
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if not os.path.isdir(full):
            continue
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        entries.append((mtime, full))
    entries.sort(key=lambda item: item[0], reverse=True)

    removed = 0
    if max_age_days and max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        stale = [(t, p) for t, p in entries if t < cutoff]
        for _, full in stale:
            try:
                shutil.rmtree(full)
                removed += 1
            except OSError:
                pass
        entries = [(t, p) for t, p in entries if t >= cutoff]

    if max_dirs and max_dirs > 0:
        for _, full in entries[max_dirs:]:
            try:
                shutil.rmtree(full)
                removed += 1
            except OSError:
                pass
    return removed


def prune_debug_dirs(dirs=None, max_files=None, max_age_days=None):
    """统一清理所有调试图片/UI 目录，返回总删除数量。"""
    from mobileauto.paths import (
        DEBUG_CLICKS_DIR,
        DEBUG_OCR_DIR,
        DEBUG_UI_DIR,
        DEBUG_PHONE_DIR,
        DEBUG_KEEP_DAYS,
        DEBUG_KEEP_MAX,
    )

    if dirs is None:
        dirs = [DEBUG_CLICKS_DIR, DEBUG_OCR_DIR, DEBUG_UI_DIR, DEBUG_PHONE_DIR]
    limit = DEBUG_KEEP_MAX if max_files is None else max_files
    days = DEBUG_KEEP_DAYS if max_age_days is None else max_age_days
    return sum(prune_files(d, limit, days) for d in dirs)
