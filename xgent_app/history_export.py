"""Chronological, human-readable exports; never used as model input."""
from __future__ import annotations

from datetime import datetime


def history_archive_files(snapshot, *, system_prompt, compression_prompt, access_logs, storage_root):
    from xgent_app.compression import (
        archive_files, attachment_index, completed, format_records, metadata_of,
        MEMORY_NAME, ATTACHMENTS_NAME, SUMMARY_NAME, _text,
    )
    import json

    files = archive_files([], [], system_prompt=system_prompt, compression_prompt=compression_prompt,
                          access_logs=access_logs, storage_root=storage_root)
    files.pop(f'1a{MEMORY_NAME}.txt')
    files.pop(f'1b{ATTACHMENTS_NAME}.txt')
    records = sorted(snapshot.get('records', []), key=lambda row: int(row.get('id') or 0))
    rounds = sorted(snapshot.get('compressions', []), key=lambda row: int(row['sequence']))
    current_epoch = int(snapshot.get('context_epoch', 0))
    resets = [row for row in records if metadata_of(row).get('context_boundary') == 'reset']
    # Only explicit reset records establish boundaries. Missing old cycles stay missing.
    epoch = max(0, current_epoch - len(resets))
    groups = {epoch: []}
    cleared_at = {}
    for row in records:
        if metadata_of(row).get('context_boundary') == 'reset':
            cleared_at[epoch] = row.get('timestamp')
            epoch += 1
            groups.setdefault(epoch, [])
        groups.setdefault(epoch, []).append(row)
    groups.setdefault(current_epoch, [])
    by_epoch = {}
    for entry in rounds:
        source_epoch = int(entry.get('context_epoch', 0))
        by_epoch.setdefault(source_epoch, []).append(entry)
        groups.setdefault(source_epoch, [])
    width = max(3, len(str(max(groups) + 1)),
                len(str(max((len(items) + 1 for items in by_epoch.values()), default=1))))
    seen_ids = set()
    all_live_ids = {row['id'] for row in records if row.get('id') is not None}
    notes = [f"会话：{snapshot.get('conversation_id', '')}",
             '文件名：阶段编号－已清空/当前－阶段内轮次 a/b/c；按名称升序阅读。',
             '已清空仅表示模型上下文已重置，保留的历史没有被删除。',
             'a：本段记录；b：对应附件路径；c：该轮实际保存的压缩结果。',
             '阶段内轮次仅用于导出排序，数据库的压缩序号不重置。',
             '旧版没有留存的记录、时间或分界不推测补造。',
             f"当前模型上下文起始记录 ID：{snapshot.get('context_start_record_id', 0)}。", '']
    current_files = None

    def write_source(prefix, rows):
        unique = []
        for row in rows:
            rid = row.get('id')
            if rid is not None:
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
            unique.append(row)
        a = f'{prefix}a{MEMORY_NAME}.txt'
        b = f'{prefix}b{ATTACHMENTS_NAME}.txt'
        files[a] = format_records(unique).encode('utf-8')
        files[b] = _text(json.dumps(attachment_index(unique, storage_root), ensure_ascii=False,
                                   indent=2)).encode('utf-8')
        return a, b

    for stage in sorted(groups):
        label = '当前' if stage == current_epoch else '已清空'
        stage_prefix = f'{stage + 1:0{width}d}-{label}'
        pending = list(groups[stage])
        entries = by_epoch.get(stage, [])
        ids = [row['id'] for row in pending if row.get('id') is not None]
        stamp = cleared_at.get(stage)
        cleared = datetime.fromtimestamp(stamp).astimezone().isoformat(timespec='seconds') if stamp else '未留存'
        notes.append(f"{stage_prefix}：记录范围 {min(ids) if ids else '空'}–{max(ids) if ids else '空'}；"
                     + ('尚未清空。' if stage == current_epoch else f'清空时间 {cleared}。'))
        for index, entry in enumerate(entries, 1):
            prefix = f'{stage_prefix}-{index:0{width}d}'
            boundary = next((row for row in pending if
                             metadata_of(row).get('context_boundary') == 'compression' and
                             metadata_of(row).get('generation') == entry.get('generation')), None)
            cutoff = boundary.get('id') if boundary else None
            if cutoff is None and entry.get('response_row_id') is not None:
                cutoff = int(entry['response_row_id']) - 1
            source = entry.get('source_records', [])
            if cutoff is None:
                known = [int(row['id']) for row in source if row.get('id') is not None]
                cutoff = max(known) if known else None
            portion = [row for row in pending if cutoff is not None and int(row.get('id') or 0) <= cutoff]
            pending = [row for row in pending if cutoff is None or int(row.get('id') or 0) > cutoff]
            # Old versions physically discarded records after compression. Export
            # only snapshots that actually survived, never reconstruct from summaries.
            legacy = [row for row in source if row.get('id') not in all_live_ids and
                      (row.get('id') is None or row['id'] not in seen_ids)]
            if legacy:
                notes.append(f'{prefix}：含实际留存的旧版压缩快照；缺失分界不作推测。')
            write_source(prefix, sorted([*legacy, *portion], key=lambda row: (row.get('timestamp') or 0, row.get('id') or 0)))
            if completed(entry):
                files[f'{prefix}c{SUMMARY_NAME}.txt'] = _text(entry['summary']).encode('utf-8')
            notes.append(f"{prefix}：数据库压缩序号 {entry['sequence']}；状态 {entry.get('status', 'completed')}。")
        tail = write_source(f'{stage_prefix}-{len(entries) + 1:0{width}d}', pending)
        if stage == current_epoch:
            current_files = tail
    files['上下文范围.txt'] = ('\n'.join(notes) + '\n').encode('utf-8')
    return dict(sorted(files.items())), current_files
