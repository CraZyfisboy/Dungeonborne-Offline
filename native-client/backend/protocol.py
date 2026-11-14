"""Exact protobuf contracts extracted from the user's original client.

This is the business-message layer. It does not claim to implement the GCloud
connection handshake, the original lobby, or any gameplay RPC by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

NATIVE = Path(__file__).resolve().parents[1]
PROJECT = NATIVE.parent
sys.path.insert(0, str(PROJECT / 'tools/python-packages'))
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


@dataclass(frozen=True)
class Rpc:
    service: str
    name: str
    input: str
    output: str
    client_streaming: bool
    server_streaming: bool

    @property
    def path(self):
        return self.service + '/' + self.name


class ClientProtocol:
    def __init__(self):
        self.source = NATIVE / 'analysis/raw-protocol/Dungeonborne/Content/AdditionalData/Protocol/Network.pb'
        self.bytes = self.source.read_bytes()
        self.fingerprint = hashlib.sha256(self.bytes).hexdigest()
        self.descriptors = descriptor_pb2.FileDescriptorSet.FromString(self.bytes)
        self.pool = descriptor_pool.DescriptorPool()
        remaining = {f.name:f for f in self.descriptors.file}
        loaded = set()
        while remaining:
            progress = []
            for name, file in remaining.items():
                if not all(dependency in loaded for dependency in file.dependency):
                    continue
                self.pool.AddSerializedFile(file.SerializeToString())
                loaded.add(name)
                progress.append(name)
            if not progress:
                raise ValueError('unresolved original protocol dependencies: ' + ', '.join(sorted(remaining)))
            for name in progress:
                del remaining[name]
        self.services = {}
        self.rpcs = {}
        for file in self.descriptors.file:
            for service in file.service:
                full = '.'.join(filter(None, (file.package, service.name)))
                methods = []
                for method in service.method:
                    rpc = Rpc(full, method.name, method.input_type.lstrip('.'), method.output_type.lstrip('.'),
                              method.client_streaming, method.server_streaming)
                    methods.append(rpc)
                    self.rpcs[rpc.path] = rpc
                self.services[full] = methods

    def message_class(self, full_name):
        return message_factory.GetMessageClass(self.pool.FindMessageTypeByName(full_name.lstrip('.')))

    def message(self, full_name, **fields):
        return self.message_class(full_name)(**fields)

    def describe(self, full_name):
        descriptor = self.pool.FindMessageTypeByName(full_name.lstrip('.'))
        return {'name':descriptor.full_name, 'fields':[
            {'name':field.name, 'number':field.number, 'type':field.type,
             'repeated':field.is_repeated, 'map':bool(field.message_type and field.message_type.GetOptions().map_entry),
             'message':field.message_type.full_name if field.message_type else None,
             'enum':field.enum_type.full_name if field.enum_type else None,
             'oneof':field.containing_oneof.name if field.containing_oneof else None}
            for field in descriptor.fields]}

    def client_rpc(self, name):
        return self.rpcs['tgf.GameClientService/' + name]
