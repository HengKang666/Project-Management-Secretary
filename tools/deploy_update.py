# -*- coding: utf-8 -*-
"""一键更新线上版本（项目管理秘书 · 47.121.183.81）。

用法（在本仓库根目录跑）：

    py -X utf8 tools/deploy_update.py --check      # 只对比本地/线上差异，**不写任何东西**
    py -X utf8 tools/deploy_update.py              # 全量更新：备份 → 上传 → 校验 → 切换 → 重启 → 自检
    py -X utf8 tools/deploy_update.py --with-env   # 连本地 .env 一起推（默认保留服务器上的 .env）
    py -X utf8 tools/deploy_update.py --no-smoke   # 重启后不跑冒烟问答

SSH 密码：环境变量 DEPLOY_SSH_PASS，否则取本地 .env 的 SERVER_SSH_PASSWORD。
**注意 .env 里的 SERVER_HOST=47.121.127.9 是已废弃的旧机**，目标机是 47.121.183.81。

为什么这么写（都是踩过的坑）：
  * 服务器上的 .env / output/ / kb_files/ 是运行态：切换前另存、切换后放回，**不能被包覆盖**。
    ★ kb_files/ 是知识库文件的磁盘原文，少了它「下载原文」就废了（台账还在 → has_raw 变 false）。
  * 上传清单（DIRS/ROOT_FILES/DEPLOY_FILES）**必须覆盖服务器上要保留的每个文件** ——
    切换是整目录替换，不在清单里的文件等于被删掉。仓库把部署脚本挪进 deploy/ 之后，
    按根目录找 start.sh / install_service.sh 会静默失败（实测被这样弄没过几个文件）。
  * 先整包传到 _staging 再整目录 mv 切换，避免"传一半"把线上弄成半新版。
  * 每个文件上传后立刻和本地 md5 比对，不一致即中止、**不进重启**。
  * 失败自动回滚：把 .bak 换回来再重启。
  * 远程路径一律用正斜杠字符串拼接（Windows 的 Path 会把 / 变反斜杠，mkdir 会拆错 → ENOENT）。
  * 停服务只用 systemctl，**绝不 pkill -f server.py**（那条命令行也含 server.py，会连自己一起杀）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
HOST = '47.121.183.81'
USER = 'root'
REMOTE = '/root/agentkownlg/deploy_server'          # systemd 的 WorkingDirectory 就在这下面
STAGING = '/root/agentkownlg/_staging_deploy'
BAK_ROOT = '/root/agentkownlg'
SERVICE = 'secretary'
PORT = 8200
KEEP_BAK = 3

# 上传范围：目录整体上，根文件按名字上。deploy/ 下的脚本与文档落到服务器根目录。
DIRS = ('secretary', 'skills', 'tools')
ROOT_FILES = ('requirements.txt', '.env.example')
# ★ 这几个文件在仓库里已经移到 deploy/ 下（早先在根目录）。
#   只按根目录找会**静默找不到**，于是每部署一次，服务器上就少一个 —— 脚本换目录时会整体替换，
#   不在上传清单里的文件等于被删掉。实测 15:07 那次部署就把 start.sh / install_service.sh
#   和《知识库与历史对话_对接手册.md》一起弄没了。
DEPLOY_FILES = ('DEPLOY.md', '部署脚本使用说明.md', '接口对接说明.md', '知识库接口文档.md',
                '技能接口文档.md', '知识库与历史对话_对接手册.md',
                'start.sh', 'start.bat', 'install_service.sh')
SKIP_DIRS = {'__pycache__', '.git', '.venv', 'node_modules', 'output'}
SKIP_SUFFIX = {'.pyc', '.pyo', '.log'}
# 这些后缀上传前统一成 LF（Linux 上跑，避免 CRLF 带来的怪问题；.bat 必须保持 CRLF，故不列）
LF_SUFFIX = {'.py', '.sh', '.md', '.json', '.html', '.txt', '.js', '.css', '.csv'}

# 运行态目录：不在上传清单里、但**必须从旧目录搬过来**，否则会被目录切换抹掉。
#   output/   —— 报告等产物
#   kb_files/ —— 知识库文件的**磁盘原文**（预览「下载原文」全靠它；丢了台账还在、has_raw 变 false）
KEEP_DIRS = ('output', 'kb_files')


def load_pass() -> str:
    p = (os.environ.get('DEPLOY_SSH_PASS') or '').strip()
    if p:
        return p
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if s.startswith('SERVER_SSH_PASSWORD='):
            return s.split('=', 1)[1].strip()
    raise SystemExit('取不到 SSH 密码：请设环境变量 DEPLOY_SSH_PASS，或在 .env 里配 SERVER_SSH_PASSWORD')


def connect() -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, username=USER, password=load_pass(),
              timeout=20, banner_timeout=20, auth_timeout=20,
              allow_agent=False, look_for_keys=False)
    return c


def run(c: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _i, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode('utf-8', 'replace')
    err = e.read().decode('utf-8', 'replace')
    return (out + err).strip()


def ignored(rels: list) -> set:
    """用仓库自己的 .gitignore 过滤：被忽略的（口径库/数据地图/metric_catalog 等业务数据）不上服务器。

    这样"什么不该外传"只在一处定义（.gitignore），不在这里再列一遍。
    """
    try:
        # 注意：input 传 bytes —— 传 str 时 Windows 会把 \n 变成 \r\n，git 收到的路径带 \r 就匹配不上
        r = subprocess.run(['git', '-C', str(ROOT), '-c', 'core.quotepath=false',
                           'check-ignore', '--stdin'],
                           input=('\n'.join(rels) + '\n').encode('utf-8'),
                           capture_output=True, timeout=60)
        out = r.stdout.decode('utf-8', 'replace')
        return {x.strip().strip('"').replace('\\', '/') for x in out.splitlines() if x.strip()}
    except Exception as e:
        print('  （git check-ignore 不可用，跳过忽略过滤：%s）' % e)
        return set()


def collect() -> list:
    """[(本地文件, 远端相对路径)]"""
    out = []
    for d in DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob('*')):
            if not p.is_file():
                continue
            if set(p.relative_to(base).parts) & SKIP_DIRS or p.suffix.lower() in SKIP_SUFFIX:
                continue
            out.append((p, p.relative_to(ROOT).as_posix()))
    for name in ROOT_FILES:
        p = ROOT / name
        if p.is_file():
            out.append((p, name))
    for name in DEPLOY_FILES:
        p = ROOT / 'deploy' / name
        if p.is_file():
            out.append((p, name))
    # 只对仓库里版本管理的两个目录做忽略过滤；根文件与 deploy 文件是按名单精挑的，不过滤
    skip = ignored([rel for _p, rel in out
                    if rel.startswith(('secretary/', 'skills/', 'tools/'))])
    if skip:
        print('  （按 .gitignore 忽略 %d 个：%s）' % (len(skip), sorted(skip)[:6]))
    return [(p, rel) for p, rel in out if rel not in skip]


def payload(p: Path) -> bytes:
    """要上传的字节。文本统一 LF —— 上传与校验都用这份字节。"""
    b = p.read_bytes()
    if p.suffix.lower() in LF_SUFFIX and b'\r\n' in b:
        b = b.replace(b'\r\n', b'\n')
    return b


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def rmdirs(sftp, path: str) -> None:
    cur = ''
    for part in [x for x in path.strip('/').split('/') if x]:
        cur = cur + '/' + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            try:
                sftp.mkdir(cur)
            except OSError:
                pass


def upload_all(c: paramiko.SSHClient, files: list) -> int:
    sftp = c.open_sftp()
    for p, rel in files:
        rp = STAGING + '/' + rel
        # 用 rpartition 取父目录、保持正斜杠：**不要** str(Path(rp).parent)（Windows 会变反斜杠）
        rmdirs(sftp, rp.rpartition('/')[0])
        with sftp.file(rp, 'wb') as f:
            f.write(payload(p))
    sftp.close()
    return len(files)


def remote_md5(c: paramiko.SSHClient, rp: str) -> str:
    return run(c, 'md5sum "' + rp + '" 2>/dev/null | cut -d" " -f1', timeout=30).split('\n')[0].strip()


def verify(c: paramiko.SSHClient, files: list) -> list:
    return [rel for p, rel in files if remote_md5(c, STAGING + '/' + rel) != md5(payload(p))]


def api(c: paramiko.SSHClient, path: str, method: str = 'GET', body: str = '', timeout: int = 60) -> str:
    if method == 'POST':
        return run(c, 'curl -s -m %d -X POST http://127.0.0.1:%d%s -H "Content-Type: application/json" '
                      '--data-binary %s' % (timeout, PORT, path, shq(body)), timeout=timeout + 20)
    return run(c, 'curl -s -m %d http://127.0.0.1:%d%s' % (timeout, PORT, path), timeout=timeout + 20)


def shq(s: str) -> str:
    """把字符串安全地包成 shell 单引号参数。"""
    return "'" + s.replace("'", "'\\''") + "'"


def health(c: paramiko.SSHClient) -> dict:
    raw = api(c, '/health', timeout=20)
    try:
        return json.loads(raw)
    except Exception:
        return {'ok': False, 'raw': raw[:300]}


def do_check(c: paramiko.SSHClient, files: list) -> int:
    print('%-44s %s' % ('文件（远端相对路径）', '状态'))
    print('-' * 60)
    diff = new = 0
    for p, rel in files:
        got = remote_md5(c, REMOTE + '/' + rel)
        if not got:
            state, new = '新文件', new + 1
        elif got != md5(payload(p)):
            state, diff = '有差异', diff + 1
        else:
            state = '一致'
        if state != '一致':
            print('%-44s %s' % (rel, state))
    print('-' * 60)
    print('%d 个文件：有差异 %d，新文件 %d，一致 %d' % (len(files), diff, new, len(files) - diff - new))
    print('服务器 .env    ：%s' % run(c, 'test -f %s/.env && echo 在 || echo 缺失' % REMOTE))
    print('服务器 skills/ ：%s' % run(c, 'test -d %s/skills && echo 在 || echo "缺失（技能接口会不可用）"' % REMOTE))
    print('服务器备份     ：%s' % run(c, 'ls -1d %s/deploy_server.bak.* 2>/dev/null | wc -l' % BAK_ROOT))
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='一键更新线上版本')
    ap.add_argument('--check', action='store_true', help='只对比本地/线上差异，不写任何东西')
    ap.add_argument('--with-env', action='store_true', help='把本地 .env 也推上去（默认保留服务器上的 .env）')
    ap.add_argument('--no-smoke', action='store_true', help='重启后不跑冒烟问答')
    args = ap.parse_args()

    files = collect()
    if not files:
        raise SystemExit('没找到要上传的文件')
    print('[0/7] 本地待上传 %d 个文件' % len(files))

    c = connect()
    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    bak = '%s/deploy_server.bak.%s' % (BAK_ROOT, ts)
    swapped = False
    try:
        if args.check:
            return do_check(c, files)

        print('[1/7] 备份当前线上版本 → %s' % bak)
        print('      ' + run(c, 'mkdir -p %s && cp -a %s %s && du -sh %s' % (BAK_ROOT, REMOTE, bak, bak)))

        print('[2/7] 清空并上传到 %s' % STAGING)
        run(c, 'rm -rf %s' % STAGING)
        print('      已上传 %d 个文件' % upload_all(c, files))

        print('[3/7] 逐文件 md5 校验')
        bad = verify(c, files)
        if bad:
            raise RuntimeError('有 %d 个文件校验不一致，已中止（未重启）：%s' % (len(bad), bad[:5]))
        print('      %d 个文件全部一致' % len(files))

        print('[4/7] 原子切换目录（保留运行态 %s%s）'
              % ('/'.join(KEEP_DIRS), '' if args.with_env else ' / .env'))
        keep = list(KEEP_DIRS) + ([] if args.with_env else ['.env'])
        for name in keep:
            print('      保留 %-10s %s' % (name, run(
                c, 'if [ -e %s/%s ]; then cp -a %s/%s %s/%s && echo ok; else echo "服务器没有，跳过"; fi'
                   % (REMOTE, name, REMOTE, name, STAGING, name))))
        if args.with_env:
            print('      推本地 .env（旧 .env 已随备份留在 %s）' % bak)
            sftp = c.open_sftp()
            with sftp.file(STAGING + '/.env', 'wb') as f:
                f.write((ROOT / '.env').read_bytes())
            sftp.close()
        print('      ' + run(c, 'mv %s %s && mv %s %s && ls -1 %s' % (REMOTE, bak, STAGING, REMOTE, REMOTE)))
        swapped = True

        print('[5/7] 依赖：requirements.txt 有变化才装')
        changed = remote_md5(c, bak + '/requirements.txt') != remote_md5(c, REMOTE + '/requirements.txt')
        if changed:
            print('      有变化 → pip install（阿里云镜像）')
            print('      ' + run(c, '/bin/python3 -m pip install -r %s/requirements.txt '
                                  '-i https://mirrors.aliyun.com/pypi/simple/ --quiet 2>&1 | tail -3' % REMOTE,
                              timeout=600))
        else:
            print('      没变化，跳过')

        print('[6/7] 重启服务并做分级自检')
        run(c, '/bin/systemctl restart %s' % SERVICE, timeout=60)
        h = {}
        for _ in range(15):
            time.sleep(2)
            h = health(c)
            if h.get('ok'):
                break
        print('      /health = %s' % h)
        if not h.get('ok'):
            raise RuntimeError('/health 不 ok：%s' % h)
        lack = [k for k in ('name_fix', 'qa_log', 'kb') if not h.get(k)]
        if lack:
            print('      ⚠ 附属能力没起来：%s' % lack)
        print('      /api/skills：%s' % api(c, '/api/skills', timeout=30)[:90])
        print('      /api/skill/title：%s' % api(c, '/api/skill/title', 'POST',
                                                 '{"skill_id":"flow-monitor","account":"sz002"}', timeout=120)[:200])

        print('[7/7] 冒烟问答' if not args.no_smoke else '[7/7] 已跳过冒烟问答')
        if not args.no_smoke:
            print('      ' + api(c, '/api/ask', 'POST',
                                 '{"question":"环潭供电所今年的线损率","history_turns":0}', timeout=180)[:220])

        print('      清理旧备份（只留最近 %d 份）：%s' % (
            KEEP_BAK, run(c, 'ls -1dt %s/deploy_server.bak.* 2>/dev/null | tail -n +%d | xargs -r rm -rf; '
                             'ls -1d %s/deploy_server.bak.* 2>/dev/null | wc -l' % (BAK_ROOT, KEEP_BAK + 1, BAK_ROOT))))
        print('')
        print('✅ 更新完成。回滚命令：')
        print('   systemctl stop %s && rm -rf %s && mv %s %s && systemctl start %s' % (SERVICE, REMOTE, bak, REMOTE, SERVICE))
        return 0
    except Exception as e:
        print('')
        print('❌ 失败：%s: %s' % (type(e).__name__, e))
        if swapped:
            print('   正在回滚 → %s' % bak)
            try:
                run(c, 'systemctl stop %s 2>/dev/null; rm -rf %s && mv %s %s && systemctl start %s'
                       % (SERVICE, REMOTE, bak, REMOTE, SERVICE), timeout=120)
                time.sleep(4)
                print('   回滚后 /health = %s' % health(c))
            except Exception as e2:
                print('   **回滚也失败了**，请手工处理：%s: %s' % (type(e2).__name__, e2))
        return 1
    finally:
        try:
            c.close()
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
