# -*- coding: utf-8 -*-
"""把 skills/*.md 同步到百炼知识库（检索面索引 r57xtq9ypm）。

用法（仓库根目录）：
    py -X utf8 tools/kb_sync.py --check    # 只列出本地与库里不一致的文档，不写
    py -X utf8 tools/kb_sync.py            # 不一致的重传（先删旧的再传，避免重名两份）

为什么要它：本地 skills/*.md 是**一处维护**的标准，但问答链路是从知识库检索标准的。
本地改了 md 只跟部署脚本上服务器，**知识库不会自动更新** —— 之前就漏传过两篇。
本工具用 md5 清单判断变化，只重传变了的。

清单：output/kb_sync_manifest.json（本地，不入库）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'secretary'))
os.chdir(str(ROOT / 'secretary'))

INDEX_ID = 'r57xtq9ypm'
MANIFEST = ROOT / 'output' / 'kb_sync_manifest.json'
# 不进知识库的：上游入参说明（接口层的东西，不是判断标准）
SKIP = {'接口文档.md'}


def local_docs() -> dict:
    out = {}
    for p in sorted((ROOT / 'skills').glob('*.md')):
        if p.name in SKIP:
            continue
        out[p.stem] = (p, hashlib.md5(p.read_bytes()).hexdigest())
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='同步 skills/*.md 到知识库')
    ap.add_argument('--check', action='store_true', help='只列差异，不上传')
    args = ap.parse_args()

    import kb_bailian
    import kb_config
    kb = kb_bailian.BailianKnowledgeBase(kb_config.settings)

    local = local_docs()
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {}
    docs = kb.list_index_documents(INDEX_ID, page_size=100).get('documents') or []
    in_kb = {}
    for d in docs:
        in_kb.setdefault((d.get('name') or '').strip(), d)

    todo = [(n, p, h) for n, (p, h) in local.items()
            if manifest.get(n) != h or n not in in_kb]
    print('本地规则文档 %d 篇；库里 %d 篇；需要同步 %d 篇' % (len(local), len(in_kb), len(todo)))
    for n, p, h in todo:
        why = '库里没有' if n not in in_kb else '本地改过'
        print('   %-30s %s' % (p.name, why))
    if args.check or not todo:
        if not todo:
            print('已一致，无需上传')
        return 0

    for n, p, h in todo:
        old = in_kb.get(n)
        if old:
            try:
                kb.delete_index_document(INDEX_ID, old.get('file_id'))
                print('   删旧版 %s' % n)
            except Exception as e:                      # noqa: BLE001
                print('   删旧版失败（继续传新的）：%s' % str(e)[:120])
        r = kb.upload_document(INDEX_ID, file_name=p.name, file_bytes=p.read_bytes(),
                               wait_parse=True, submit_index=True, skip_if_exists=False)
        print('   上传 %-30s %s' % (p.name, r.get('parse_status')))
        manifest[n] = h
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    print('清单已更新：%s' % MANIFEST)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
