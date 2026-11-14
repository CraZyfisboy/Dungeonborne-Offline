"""Launch the original client and the local save service as one session."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading

NATIVE = Path(__file__).resolve().parents[1]
PROJECT = NATIVE.parent
STAGE = NATIVE / 'runtime-headless'
BIN = STAGE / 'Dungeonborne/Binaries/Win64'
PAKS = STAGE / 'Dungeonborne/Content/Paks'
EXE = BIN / 'Dungeonborne.exe'
SAVE = NATIVE / 'local-save/profile.sqlite3'
LATEST = NATIVE / 'local-save/session.json'
EXE_SHA = '62d509a00d984696e021fb5ddba9f87fd1b27c84f695877345ff5fe9401f58b3'
sys.path[:0] = [str(NATIVE / 'tools'), str(NATIVE / 'backend')]
from build_loot_package import build
from server import open_service, receive_exact


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def read_report(name):
    return json.loads((NATIVE / 'reports' / name).read_text(encoding='utf8'))


def checked_asset(path,expected):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(NATIVE / 'artifacts') or checksum(path) != expected:
        raise ValueError('本机补丁文件校验失败：' + path.name)
    return path


def artifacts():
    if not EXE.is_file() or checksum(EXE)!=EXE_SHA:
        raise ValueError('独立运行副本不是已支持的 Dungeonborne 1.0.0.97')
    marker=read_report('lua-marker-preparation.json')
    client=read_report('native-probe-preparation.json')
    adapter=read_report('adapter-build.json')
    if not marker['independently_read_back'] or not client['independently_read_back'] or adapter['exit_code']:
        raise ValueError('原版客户端补丁尚未完成独立校验')
    return (
        (checked_asset(marker['marker_package'],marker['package_sha256']),PAKS / 'pakchunk10000-OfflineDiagnostics_P.pak'),
        (checked_asset(client['package'],client['package_sha256']),PAKS / Path(client['package']).name),
        (checked_asset(NATIVE / 'artifacts/offline-native-adapter.dll',adapter['output_sha256']),
         BIN / 'offline-native-adapter.dll'),
    )


def game_is_running():
    result=subprocess.run(['powershell','-NoProfile','-Command',
                           '@(Get-Process -Name Dungeonborne -ErrorAction SilentlyContinue).Count'],
                          capture_output=True,text=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    return result.stdout.strip()!='0'


def seed_save(save):
    if save.exists() or save!=SAVE:
        return
    source=NATIVE / 'reports/backend-integration/test-profile.sqlite3'
    if not source.is_file():
        return
    save.parent.mkdir(parents=True,exist_ok=True)
    backups=save.parent / 'backups'
    backups.mkdir(exist_ok=True)
    backup=backups / (datetime.now().strftime('%Y%m%d-%H%M%S')+'-pre-migration.sqlite3')
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as original, sqlite3.connect(backup) as copy:
        original.backup(copy)
        if copy.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('旧测试存档备份未通过完整性检查')
    shutil.copy2(backup,save)


def current_console():
    try:
        latest=json.loads(LATEST.read_text(encoding='utf8'))
        path=Path(latest['console']).resolve(strict=True)
        if not path.is_relative_to(NATIVE / 'reports/local-play'):
            return None
        receipt=json.loads(path.read_text(encoding='utf8'))
        if Path(receipt['save']).resolve()!=SAVE:
            return None
        with socket.create_connection(('127.0.0.1',18897),timeout=0.5) as connection:
            connection.sendall(b'DBLOCAL1')
            if receive_exact(connection,8)==b'DBLOCAL1':
                return receipt['url']
    except (OSError,ValueError,KeyError):
        return None


@contextmanager
def launch_lock():
    path = LATEST.with_name('launch.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError('单机版或设置控制台正在运行。关闭当前窗口后可以重新启动。') from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def require_local_ports():
    for port in (18897, 18898):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError(f'本机端口 {port} 已有程序在监听，无法启动存档服务。')


def run_session(options, save):
    if (NATIVE / 'tools/release/resource-manifest.json').is_file() and not (NATIVE / 'local-assets/install.json').is_file():
        raise ValueError('请先双击「安装单机 MOD.cmd」完成安装。')
    if not options.console and game_is_running():
        raise ValueError('Dungeonborne 已经打开。关闭该进程后再启动本机单机版。')
    require_local_ports()
    seed_save(save)
    if not options.console:
        for source,destination in artifacts():
            shutil.copy2(source,destination)
        build(save,STAGE)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)
    output=NATIVE / 'reports/local-play' / stamp
    output.mkdir(parents=True)
    events=None
    if not options.console:
        events=BIN / ('offline-events-'+secrets.token_hex(12))
        events.mkdir()
    receipt=output / 'console.json'
    with open_service(save=save, log=output/'backend.jsonl', receipt=receipt,
                      native_events=events) as (server, url):
        session={'save':str(save),'console':str(receipt),'backend_pid':os.getpid()}
        if events:
            session['events']=str(events)
        (output/'session.json').write_text(json.dumps(session,ensure_ascii=False,indent=2),encoding='utf8')
        if save==SAVE:
            LATEST.write_text(json.dumps(session,ensure_ascii=False,indent=2),encoding='utf8')
        print('本机控制台（复制到浏览器打开）：' + url,flush=True)
        if options.console:
            print('关闭这个窗口或按 Ctrl+C 可停止控制台。',flush=True)
            server.serve_forever(poll_interval=0.2)
            return
        launch_game(options, save, events, output, session, server)


def game_arguments(save, output, headless=False):
    user=save.parent / 'game-user'
    user.mkdir(exist_ok=True)
    args=[str(EXE),'/TgfMaps/Scenes/Dungeon/L_Dungeon_Official_01','-nosteam','-multihome=127.0.0.1',
          '-port=17777','-DsIsStandAlone','-NoCrashDialog','-nosplash','-log','-stdout',
          '-abslog='+str(output/'engine.log'),'-UserDir='+str(user)]
    if headless:
        args+=['-server','-nullrhi','-unattended','-nosound']
    return args


def game_environment(events):
    env={k:v for k,v in os.environ.items() if not k.startswith(('DB_NATIVE_','DB_ADAPTER_','DB_LOCAL_'))}
    env.update(DB_NATIVE_EVENT_DIR=events.name,DB_NATIVE_PRODUCT='1',DB_ADAPTER_KEEP_RUNNING='1',
               DB_NATIVE_WAITING_ENTRY='1',DB_NATIVE_BOOT_ROUTE='original-lobby')
    return env


def launch_game(options, save, events, output, session, server):
    service_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.2}, daemon=True)
    service_thread.start()
    try:
        with (output/'game-stdout.log').open('w',encoding='utf8') as stream:
            game=subprocess.Popen(game_arguments(save, output, options.headless_seconds),cwd=BIN,
                                  env=game_environment(events),stdout=stream,stderr=subprocess.STDOUT,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
            session['game_pid']=game.pid
            (output/'session.json').write_text(json.dumps(session,ensure_ascii=False,indent=2),encoding='utf8')
            try:
                rc=game.wait(timeout=options.headless_seconds or None)
            except subprocess.TimeoutExpired:
                game.terminate()
                game.wait(timeout=10)
                rc=0
        if rc:
            raise ValueError('原版客户端异常退出，日志：' + str(output/'engine.log'))
    finally:
        server.shutdown()
        service_thread.join()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--console',action='store_true',help='只启动本机设置面板')
    parser.add_argument('--check',action='store_true',help='校验客户端和补丁，不启动')
    parser.add_argument('--headless-seconds',type=int,default=0,help=argparse.SUPPRESS)
    parser.add_argument('--save',type=Path,default=SAVE,help=argparse.SUPPRESS)
    options=parser.parse_args()
    save=options.save.resolve()
    if not save.is_relative_to(NATIVE) or (options.headless_seconds and not 20<=options.headless_seconds<=90):
        parser.error('无效的测试存档或运行时间')
    if options.check:
        artifacts()
        print('原版客户端及本机补丁校验通过。')
        return
    if options.console and save==SAVE:
        url=current_console()
        if url:
            print('本机控制台：' + url)
            return
    try:
        with launch_lock():
            run_session(options, save)
    except KeyboardInterrupt:
        pass


if __name__=='__main__':
    try:
        main()
    except (ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(1)
