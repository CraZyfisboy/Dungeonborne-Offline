"""Load the required original resources from the supported Steam build."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory


HERE = Path(__file__).resolve().parent
NATIVE = HERE.parent.parent
PROJECT = NATIVE.parent
CONTENT = "Dungeonborne/Content"
PAKS = Path(CONTENT) / "Paks"
EXE = Path("Dungeonborne/Binaries/Win64/Dungeonborne.exe")
CONFIG = PROJECT / "analysis/raw-config" / CONTENT / "AdditionalData"
SCRIPT = NATIVE / "analysis/raw-lua"
NETWORK = NATIVE / "analysis/raw-protocol" / CONTENT / "AdditionalData/Protocol/Network.pb"
STOCK = NATIVE / "local-assets/trade-stock.bin"


def digest(file, start=0, size=None):
    sha = hashlib.sha256()
    file.seek(start)
    remaining = size
    while remaining is None or remaining > 0:
        block = file.read(min(4 * 1024 * 1024, remaining)
                          if remaining is not None else 4 * 1024 * 1024)
        if not block:
            break
        sha.update(block)
        if remaining is not None:
            remaining -= len(block)
    if remaining:
        raise ValueError("Steam 文件被截断。请使用 Steam 的验证游戏文件功能。")
    return sha.hexdigest()


def read_exact(file, size):
    block = file.read(size)
    if len(block) != size:
        raise ValueError("文件内容不完整。")
    return block


def read_resources(staging, manifest):
    """Run the standalone decoder only on verified, unencrypted Steam blocks."""
    path = staging / PAKS / manifest["archive"]
    entries = manifest["entries"]
    result = {}
    with path.open("rb") as archive, tempfile.TemporaryDirectory(prefix="resources-", dir=NATIVE / "local-assets") as tmp:
        if digest(archive) != manifest["sha256"]:
            raise ValueError("Steam 资源包版本不符或已损坏，请验证 Dungeonborne 1.0.0.97 文件。")
        directory = Path(tmp)
        packed_entries = []
        with (directory / "block.input").open("wb") as feed:
            for entry in entries:
                if entry["encrypted"] or entry["archive"] != manifest["archive"]:
                    raise ValueError("安装清单包含不支持的资源。")
                if entry["method"] == "None":
                    archive.seek(entry["offset"])
                    result[entry["path"]] = read_exact(archive, entry["size"])
                    continue
                if entry["method"] != "Oodle" or not entry["blocks"]:
                    raise ValueError("安装清单包含未支持的压缩格式。")
                remaining = entry["size"]
                for block in entry["blocks"]:
                    archive.seek(block["start"])
                    data = read_exact(archive, block["end"] - block["start"])
                    unpacked = min(remaining, entry["block_size"])
                    feed.write(struct.pack("<II", len(data), unpacked))
                    feed.write(data)
                    remaining -= unpacked
                if remaining:
                    raise ValueError("安装清单的资源块不完整。")
                packed_entries.append(entry)
        run = subprocess.run([str(HERE / "decoder.exe")], cwd=directory, timeout=60,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        if run.returncode:
            raise ValueError(f"无法读取原版游戏资源，解压器退出码 {run.returncode}。")
        with (directory / "block.output").open("rb") as raw:
            for entry in packed_entries:
                result[entry["path"]] = read_exact(raw, entry["size"])
            if raw.read(1):
                raise ValueError("资源读取长度不符。")
    for entry in entries:
        if hashlib.sha256(result[entry["path"]]).hexdigest() != entry["sha256"]:
            raise ValueError("原版资源校验失败：" + entry["path"])
    return result


def write_resources(resources, table_names):
    configure = resources[CONTENT + "/AdditionalData/Protocol/Configure.pb"]
    pool = descriptor_pool.DescriptorPool()
    for descriptor in descriptor_pb2.FileDescriptorSet.FromString(configure).file:
        pool.Add(descriptor)
    blocks_type = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("org.xresloader.pb.xresloader_datablocks"))
    tables, strings = {}, {}
    for path, data in resources.items():
        if path.startswith(CONTENT + "/Script/"):
            dest = SCRIPT / path
        elif path == CONTENT + "/AdditionalData/Protocol/Network.pb":
            dest = NETWORK
        else:
            relative = path.removeprefix(CONTENT + "/AdditionalData/")
            dest = CONFIG / relative
            if relative in table_names:
                blocks = blocks_type.FromString(data)
                cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(blocks.data_message_type))
                if blocks.HasField("header") and len(blocks.data_block) != blocks.header.count:
                    raise ValueError("游戏配置行数错误：" + relative)
                tables[table_names[relative]] = [
                    json_format.MessageToDict(cls.FromString(row), preserving_proto_field_name=True,
                                              always_print_fields_with_no_presence=True)
                    for row in blocks.data_block
                ]
            elif relative.startswith("StringTable/"):
                lines = csv.reader(data.decode("utf-8-sig").splitlines(keepends=True))
                if next(lines) != ["Key", "SourceString"]:
                    raise ValueError("游戏文本表头不符：" + relative)
                strings[Path(path).name] = dict(lines)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    if set(tables) != set(table_names.values()) or set(strings) != {"ItemStringTable.csv", "GeneralStringTable.csv"}:
        raise ValueError("原版配置不完整。")
    cache = PROJECT / "runtime/_data/cache/config"
    cache.mkdir(parents=True, exist_ok=True)
    for name, value in (("tables", tables), ("strings", strings)):
        (cache / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def verify_patch(path, expected):
    """Read the installed version-3 pak index and compare its contents."""
    with path.open("rb") as file:
        file.seek(-44, 2)
        end = file.tell()
        magic, version, start, length = struct.unpack("<IIqq", read_exact(file, 24))
        checksum = read_exact(file, 20)
        if magic != 0x5A6F12E1 or version != 3 or not (0 <= start < end and start + length == end):
            raise ValueError("补丁资源包格式不符。")
        file.seek(start)
        index = read_exact(file, length)
        if hashlib.sha1(index).digest() != checksum:
            raise ValueError("补丁资源包目录校验失败。")
        from io import BytesIO
        directory = BytesIO(index)

        def string():
            size, = struct.unpack("<i", read_exact(directory, 4))
            if not 1 <= size <= 1024:
                raise ValueError("补丁资源包的路径无效。")
            return read_exact(directory, size).removesuffix(b"\0").decode("ascii")

        if string() != "../../../":
            raise ValueError("补丁挂载目录不符。")
        count, = struct.unpack("<i", read_exact(directory, 4))
        if count != len(expected):
            raise ValueError("补丁文件数不符。")
        seen = set()
        for _ in range(count):
            name = string()
            entry = read_exact(directory, 53)
            offset, stored, raw, compression, sha, flags, chunk = struct.unpack("<qqqI20sBI", entry)
            payload = expected.get(name)
            if (payload is None or name in seen or stored != len(payload) or raw != stored
                    or not 0 <= offset <= start - 53 - stored or compression or flags or chunk):
                raise ValueError("补丁目录内容不符：" + name)
            file.seek(offset)
            if read_exact(file, 53) != entry:
                raise ValueError("补丁条目校验失败：" + name)
            data = read_exact(file, stored)
            if data != payload or hashlib.sha1(data).digest() != sha:
                raise ValueError("补丁内容校验失败：" + name)
            seen.add(name)
        if directory.read(1):
            raise ValueError("补丁目录末尾不符。")
