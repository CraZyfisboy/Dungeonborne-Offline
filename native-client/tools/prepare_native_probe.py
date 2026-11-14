"""Prepare a hash-bound original-client staging tree and one reversible data patch.

The installed game is read-only. Content files are linked for read access; all
executables/libraries are independent copies. Never patch a linked content file.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
INSTALL = Path(r"E:\Steam\steamapps\common\Dungeonborne")
STAGING = ROOT / "runtime"
sys.path.insert(0, str(PROJECT / "tools/python-packages"))
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fstring(value):
    data = value.encode("ascii") + b"\0"
    return struct.pack("<i", len(data)) + data


def pak_entry(offset, data):
    return struct.pack("<qqqI", offset, len(data), len(data), 0) + hashlib.sha1(data).digest() + struct.pack("<BI", 0, 0)


def pak(files):
    # Legacy version 3, uncompressed and unencrypted. Both the entry and index
    # structures are independently checked with CUE4Parse before any game probe.
    stream = io.BytesIO()
    entries = []
    for path, data in sorted(files.items()):
        offset = stream.tell()
        entry = pak_entry(offset, data)
        stream.write(entry)
        stream.write(data)
        entries.append((path, entry))
    index = fstring("../../../") + struct.pack("<i", len(entries))
    index += b"".join(fstring(path) + entry for path, entry in entries)
    offset = stream.tell()
    stream.write(index)
    stream.write(struct.pack("<IIqq", 0x5A6F12E1, 3, offset, len(index)) + hashlib.sha1(index).digest())
    return stream.getvalue()


def patch_config():
    raw = PROJECT / "analysis/raw-config/Dungeonborne/Content/AdditionalData"
    source = raw / "ExcelConfig/const.bytes"
    schemas = descriptor_pb2.FileDescriptorSet.FromString((raw / "Protocol/Configure.pb").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for descriptor in schemas.file:
        pool.Add(descriptor)
    envelope_type = message_factory.GetMessageClass(pool.FindMessageTypeByName("org.xresloader.pb.xresloader_datablocks"))
    original = envelope_type.FromString(source.read_bytes())
    row_type = message_factory.GetMessageClass(pool.FindMessageTypeByName(original.data_message_type))
    changed = envelope_type()
    changed.CopyFrom(original)
    modified = []
    for index, block in enumerate(original.data_block):
        row = row_type.FromString(block)
        if row.key == "stand_alone_disable":
            if row.value != "TRUE":
                raise ValueError("standalone flag differs from the inspected build")
            row.value = "FALSE"
            changed.data_block[index] = row.SerializeToString(deterministic=True)
            modified.append(index)
    if len(modified) != 1:
        raise ValueError("expected exactly one standalone flag")
    before_rows = [row_type.FromString(block) for block in original.data_block]
    after_rows = [row_type.FromString(block) for block in changed.data_block]
    after_rows[modified[0]].value = "TRUE"
    if before_rows != after_rows:
        raise ValueError("unintended configuration changes")
    data = changed.SerializeToString(deterministic=True)
    relative = "Dungeonborne/Content/AdditionalData/ExcelConfig/const.bytes"
    target = ROOT / "payload" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    package = ROOT / "artifacts/pakchunk9999-OfflineNative_P.pak"
    package.parent.mkdir(exist_ok=True)
    package.write_bytes(pak({relative: data}))
    return dict(source=str(source), source_sha256=digest(source), payload=str(target), payload_sha256=digest(target),
                package=str(package), package_sha256=digest(package), row_count=len(before_rows),
                changed_key="stand_alone_disable", old_value="TRUE", new_value="FALSE",
                semantic_changes=1, independently_read_back=False, original_client_playability="unverified")


def stage():
    inventory = json.loads((PROJECT / "reports/original-installation-final-20261110-2316.json").read_text(encoding="utf-8"))
    if Path(inventory["game_root"]) != INSTALL:
        raise ValueError("installation mismatch")
    if STAGING.resolve().is_relative_to(INSTALL.resolve()):
        raise ValueError("staging must not be inside original installation")
    rows = []
    for item in inventory["files"]:
        relative = Path(item["path"])
        source = INSTALL / relative
        target = STAGING / relative
        if not source.resolve().is_relative_to(INSTALL.resolve()) or not target.resolve().is_relative_to(STAGING.resolve()):
            raise ValueError("invalid staging path")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = relative.as_posix().startswith("Dungeonborne/Content/Paks/")
        if not target.exists():
            if content:
                os.link(source, target)
            else:
                shutil.copy2(source, target)
        if digest(target) != item["sha256"]:
            raise ValueError(f"staged file differs from inspected original: {relative}")
        if not content and os.path.samefile(source, target):
            raise ValueError("executable or library must be an independent copy")
        rows.append(dict(path=relative.as_posix(), kind="content-read-link" if content else "independent-copy", sha256=item["sha256"]))
    return rows


def main():
    report = patch_config()
    report["staged_files"] = stage()
    report["staging_root"] = str(STAGING)
    (ROOT / "reports/native-probe-preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Prepared {len(report['staged_files'])} original files and a one-key data patch; original-client playability is unverified.")


if __name__ == "__main__":
    main()
