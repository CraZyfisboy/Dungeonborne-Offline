"""Prepare a fresh offline install using the player's original Steam copy."""
from __future__ import annotations

import argparse
from ctypes import windll
import filecmp
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg


RELEASE = Path(__file__).resolve().parent
NATIVE = RELEASE.parent.parent
PROJECT = NATIVE.parent
sys.path[:0] = [str(NATIVE / "backend"), str(NATIVE / "tools"),
                str(PROJECT / "tools/python-packages")]
from launch_offline import (BIN, STAGE, artifacts, checksum, current_console, game_arguments,
                            game_environment, game_is_running, launch_lock, require_local_ports)
from build_loot_package import build
from game_data import GameData
from market import TradeHandlers
from protocol import ClientProtocol
from release.resources import PAKS, STOCK, digest, read_resources, verify_patch, write_resources
from server import open_service
from trade_armor import additional_trade_rows


MANIFEST = RELEASE / "resource-manifest.json"
RECEIPT = NATIVE / "local-assets/install.json"


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    temp.replace(path)


def find_game():
    candidates = []
    for hive, key_path, name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                candidates.append(Path(winreg.QueryValueEx(key, name)[0]))
        except OSError:
            pass
    candidates.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    for drive in os.listdrives():
        if windll.kernel32.GetDriveTypeW(drive) == 3:
            candidates.extend(Path(drive) / name for name in ("Steam", "SteamLibrary"))
    game_folders = set()
    for steam in dict.fromkeys(candidates):
        libraries = [steam]
        vdf = steam / "steamapps/libraryfolders.vdf"
        if vdf.is_file():
            import re
            try:
                text = vdf.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                text = ""
            libraries.extend(Path(path.replace("\\\\", "\\"))
                             for path in re.findall(r'"path"\s+"([^"]+)"', text))
        for library in libraries:
            game = library / "steamapps/common/Dungeonborne"
            if (game / "Dungeonborne/Binaries/Win64/Dungeonborne.exe").is_file():
                game_folders.add(game.resolve())
    if len(game_folders) == 1:
        game, = game_folders
        print("已找到 Steam 游戏目录：" + str(game), flush=True)
        return game
    if game_folders:
        print("检测到多个游戏目录，请选择当前 Steam 安装。", flush=True)
    print("请打开 Steam 的 Dungeonborne → 管理 → 浏览本地文件，复制该文件夹路径。", flush=True)
    return Path(input("游戏目录：").strip().strip('"')).resolve(strict=True)


def validate_source(game, manifest):
    if PROJECT.is_relative_to(game) or game.is_relative_to(PROJECT):
        raise ValueError("请把 MOD 解压到游戏目录之外，并选择 Steam 中原来的游戏目录。")
    missing = []
    for entry in manifest["game_files"]:
        path = game / entry["path"]
        if not path.is_file() or not path.resolve().is_relative_to(game) or path.stat().st_size != entry["size"]:
            missing.append(entry["path"])
    if missing:
        raise ValueError("游戏版本不符或文件不完整。请在 Steam 中验证 1.0.0.97 的文件。\n" +
                         "\n".join(missing[:6]))
    for entry in manifest["game_files"]:
        if entry["path"].startswith(PAKS.as_posix() + "/") or entry["path"].endswith("Win64/Dungeonborne.exe"):
            if checksum(game / entry["path"]) != entry["sha256"]:
                raise ValueError("游戏版本不支持，或 Steam 文件已损坏：" + entry["path"])


def stage_game(game, manifest):
    # Never patch the user's Steam tree. Only the immutable cooked archives
    # may share a file with the original on the same volume.
    STAGE.mkdir(parents=True, exist_ok=True)
    if STAGE.resolve() != STAGE or any(p.is_symlink() or p.is_junction()
                                      for p in STAGE.rglob("*") if p.is_dir()):
        raise ValueError("离线运行目录不能是其他文件夹的链接。")
    for entry in manifest["game_files"]:
        relative = Path(entry["path"])
        src, dst = game / relative, STAGE / relative
        if not dst.resolve().is_relative_to(STAGE) or dst.is_symlink():
            raise ValueError("离线运行目录包含无效文件链接。")
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = relative.is_relative_to(PAKS)
        if dst.exists():
            if (content and os.path.samefile(src, dst)
                    or not content and not os.path.samefile(src, dst)
                    and filecmp.cmp(src, dst, shallow=False)):
                continue
            if content and dst.stat().st_size == src.stat().st_size and checksum(dst) == entry["sha256"]:
                continue
        temp = dst.with_name(dst.name + ".stage-" + secrets.token_hex(5))
        try:
            if content:
                try:
                    os.link(src, temp)
                except OSError:
                    shutil.copy2(src, temp)
            else:
                shutil.copy2(src, temp)
            temp.replace(dst)
        finally:
            temp.unlink(missing_ok=True)


def check_space(game, manifest):
    shared = game.stat().st_dev == PROJECT.stat().st_dev
    extra = 48 * 1024 * 1024
    for entry in manifest["game_files"]:
        relative = Path(entry["path"])
        if (STAGE / relative).exists() or shared and relative.is_relative_to(PAKS):
            continue
        extra += entry["size"]
    if shutil.disk_usage(PROJECT).free < extra:
        raise ValueError(f"空间不足，安装还需要约 {extra / (1024 ** 3):.1f} GiB。建议放到 Steam 游戏所在盘。")


def install_patches():
    reports = NATIVE / "reports"
    (NATIVE / "artifacts").mkdir(exist_ok=True)
    reports.mkdir(exist_ok=True)
    from prepare_native_probe import patch_config
    base = patch_config()
    source = Path(base["package"])
    installed = STAGE / PAKS / source.name
    shutil.copy2(source, installed)
    verify_patch(installed, {"Dungeonborne/Content/AdditionalData/ExcelConfig/const.bytes":
                             Path(base["payload"]).read_bytes()})
    base["independently_read_back"] = True
    write_json(reports / "native-probe-preparation.json", base)

    subprocess.run([sys.executable, "-B", "-X", "utf8", str(NATIVE / "tools/prepare_lua_marker.py"), "--player"],
                   check=True, cwd=PROJECT, creationflags=subprocess.CREATE_NO_WINDOW)
    path = reports / "lua-marker-preparation.json"
    marker = json_file(path)
    source = Path(marker["marker_package"])
    installed = STAGE / PAKS / source.name
    shutil.copy2(source, installed)
    verify_patch(installed, {name: (NATIVE / "payload-lua-marker" / name).read_bytes()
                             for name in marker["expected_payloads"]})
    marker["independently_read_back"] = True
    write_json(path, marker)

    adapter = NATIVE / "artifacts/offline-native-adapter.dll"
    if checksum(adapter) != json_file(RELEASE / "build-info.json")["adapter_sha256"]:
        raise ValueError("安装包中的本机组件校验失败，请重新解压 ZIP。")
    write_json(reports / "adapter-build.json", {"exit_code": 0, "output_sha256": checksum(adapter)})
    for source, destination in artifacts():
        shutil.copy2(source, destination)


def stock_ids(data):
    rows = data.tables["excel_trade_commodity"] + additional_trade_rows(data)
    return {r["item_id"] for r in rows if r["is_on_shelf"] and r["price_def"] > 0}


def verify_stock(path, protocol, data):
    market = TradeHandlers()
    market.protocol = protocol
    market.game_data = data
    market.initialize_trade(path)
    variants = set(data.equipment_balance.added_items)
    if not variants.issubset(market.trade_templates) or market.trade_templates.keys() != stock_ids(data):
        raise ValueError(f"交易行商品生成不完整（{len(market.trade_templates)} 件）。")
    return len(market.trade_templates)


def generate_stock(protocol, data, log):
    for name in ("offline-market-stock-install.bin", "offline-market-stock-install.tmp",
                 "offline-native-action.txt", "offline-adapter-attached.txt"):
        (BIN / name).unlink(missing_ok=True)
    (BIN / "offline-market-stock-ids.txt").write_text(
        "\n".join(map(str, sorted(stock_ids(data)))) + "\n", encoding="ascii", newline="\n")
    events = BIN / ("offline-events-" + secrets.token_hex(12))
    events.mkdir()
    ready = BIN / "offline-market-stock-install.bin"
    with tempfile.TemporaryDirectory(prefix="setup-", dir=NATIVE / "local-assets") as directory:
        root = Path(directory)
        save = root / "profile.sqlite3"
        build(save, STAGE)
        with open_service(save=save, log=log / "backend.jsonl", receipt=root / "console.json",
                          native_events=events) as (server, _):
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                env = game_environment(events)
                env["DB_LOCAL_INSTALL_STOCK"] = "1"
                with (log / "game-stdout.log").open("w", encoding="utf-8") as stream:
                    with subprocess.Popen(game_arguments(save, log, headless=True), cwd=BIN, env=env,
                                          stdout=stream, stderr=subprocess.STDOUT,
                                          creationflags=subprocess.CREATE_NO_WINDOW) as game:
                        try:
                            end = time.monotonic() + 45
                            while not ready.is_file() and game.poll() is None and time.monotonic() < end:
                                time.sleep(0.2)
                            if not ready.is_file():
                                raise ValueError("首次启动未能生成交易行商品，日志位置：" + str(log))
                            count = verify_stock(ready, protocol, data)
                            ready.replace(STOCK)
                        finally:
                            if game.poll() is None:
                                game.terminate()
                                game.wait(timeout=10)
            finally:
                server.shutdown()
                worker.join()
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", type=Path, help="原版 Steam 游戏目录")
    args = parser.parse_args()
    manifest = json_file(MANIFEST)
    game = args.game.resolve(strict=True) if args.game else find_game()
    with launch_lock():
        if game_is_running() or current_console():
            raise ValueError("请关闭游戏和本机设置控制台，再运行安装。")
        require_local_ports()
        build_info = json_file(RELEASE / "build-info.json")
        if checksum(RELEASE / "decoder.exe") != build_info["decoder_sha256"]:
            raise ValueError("安装包中的资源读取组件校验失败，请重新解压 ZIP。")
        print("正在检查 Steam 文件。", flush=True)
        validate_source(game, manifest)
        check_space(game, manifest)
        print("正在准备独立运行目录。跨盘时需要复制游戏文件。", flush=True)
        stage_game(game, manifest)
        (NATIVE / "local-assets").mkdir(exist_ok=True)
        write_resources(read_resources(STAGE, manifest["resources"]), manifest["tables"])
        install_patches()
        protocol = ClientProtocol()
        data = GameData(protocol)
        log = NATIVE / "reports/install" / secrets.token_hex(6)
        log.mkdir(parents=True)
        print("正在首次启动原版客户端，生成本机交易行商品。", flush=True)
        count = generate_stock(protocol, data, log)
        write_json(RECEIPT, {"version": manifest["version"], "game": str(game), "trade_items": count,
                             "trade_sha256": checksum(STOCK)})
        print(f"安装完成，交易行已生成 {count} 种装备。请双击「启动单机.cmd」。", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, EOFError) as error:
        print(f"安装失败：{error}", file=sys.stderr, flush=True)
        sys.exit(1)
