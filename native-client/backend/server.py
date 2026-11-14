"""Loopback-only endpoint for the native adapter's framed, original CSMsgs.

The outer four-byte framing belongs to our adapter. It is not an emulation of
GCloud's transport, authentication servers, or remote service infrastructure.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import socketserver
import select
import struct
import sys
import threading
import os
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console import PlayerConsole
from game_data import GameData
from native_events import NativeEvents
from protocol import ClientProtocol, NATIVE
from service import LocalService, Session, pack_frames
from state import StateStore


REFRESH_INVENTORY_RPCS={
    'trade_purchase_order_publish','trade_sales_order_publish','trade_auction_order_publish',
    'trade_bid_order_publish','trade_order_cancel','trade_receive_confirmation_product',
    'market_buy_product','market_sell_items','inventory_item_move','inventory_item_swap',
    'role_wear','role_take_off','role_weapon_set_swap',
    'gear_dismantle','gear_heirloom_instantiation','gear_heirloom_unload',
    'crafting','crafting_cancel','crafting_recived',
}


def receive_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            if result:
                raise ValueError('local connection ended inside a frame')
            return None
        result.extend(block)
    return bytes(result)


class LocalServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True

    def service_actions(self):
        if self.native_events is not None:
            self.native_events.poll()
        now=time.monotonic()
        if now>=self.next_market_check:
            self.next_market_check=now+1
            completed=self.service.process_trade()
            crafted=self.service.process_crafting()
            if (completed or crafted) and self.native_events is not None:
                self.native_events.refresh_inventory()
            if completed:
                self.event({'event':'virtual_market_sales','tickets':completed})

    def event(self, data):
        with self.log_lock:
            data = {'utc': datetime.now(timezone.utc).isoformat(), **data}
            with self.log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(data, ensure_ascii=False) + '\n')


class NativeConnection(socketserver.BaseRequestHandler):
    def handle(self):
        session = Session()
        self.request.settimeout(60)
        self.server.event({'event': 'connected', 'session': session.key})
        try:
            if receive_exact(self.request, 8) != b'DBLOCAL1':
                raise ValueError('not the local native adapter')
            self.request.sendall(b'DBLOCAL1')
            while True:
                if not select.select([self.request],[],[],0.2)[0]:
                    frames=self.server.service.drain_notices(session)
                    if frames:
                        self.request.sendall(pack_frames(frames))
                    continue
                header = receive_exact(self.request, 4)
                if header is None:
                    break
                length = struct.unpack('!I', header)[0]
                if not 0 < length <= 4 * 1024 * 1024:
                    raise ValueError('local frame size outside allowed range')
                packet = receive_exact(self.request, length)
                if packet is None:
                    raise ValueError('local connection omitted a frame body')
                frames, outcome = self.server.service.dispatch(packet, session)
                frames.extend(self.server.service.drain_notices(session))
                self.server.event({'event': 'rpc', 'session': session.key,
                                   'request_bytes': len(packet), 'response_bytes': sum(map(len, frames)), **outcome})
                self.request.sendall(pack_frames(frames))
                if (self.server.native_events and not outcome['error'] and not outcome['replayed']
                    and (outcome.get('coin_changed') or outcome['rpc'] in REFRESH_INVENTORY_RPCS)):
                    self.server.native_events.refresh_inventory(outcome.get('inventory_updates', ()))
        except Exception as exc:
            self.server.event({'event': 'connection_error', 'session': session.key, 'error': str(exc)})
        finally:
            self.server.event({'event': 'disconnected', 'session': session.key})


@contextmanager
def open_service(*, save, log, receipt, native_events=None, port=18897, console_port=18898):
    for path in (save, log, receipt):
        if not path.resolve().is_relative_to(NATIVE):
            raise ValueError('save and log must remain within this native project')
    events = None
    if native_events is not None:
        events = native_events.resolve()
        allowed = {NATIVE / name / 'Dungeonborne/Binaries/Win64' for name in ('runtime','runtime-headless')}
        if events.parent not in allowed or not events.name.startswith('offline-events-') or not events.is_dir():
            raise ValueError('native event directory must be an isolated project launch directory')

    protocol = ClientProtocol()
    game_data = GameData(protocol)
    with closing(StateStore(save, protocol, game_data)) as store, LocalServer(('127.0.0.1', port), NativeConnection) as server:
        server.log = log.resolve()
        server.log.parent.mkdir(parents=True, exist_ok=True)
        server.log_lock = threading.Lock()
        server.service = LocalService(protocol, store, game_data)
        server.next_market_check = 0
        server.native_events = NativeEvents(events, server.service, server.event) if events else None
        if server.native_events:
            server.native_events.poll()
            with store.transaction() as tx:
                tx.db.execute("UPDATE local_runs SET status='interrupted' WHERE status IN ('active','reserved')")

        with PlayerConsole(('127.0.0.1', console_port), server.service) as console:
            console_thread = threading.Thread(target=console.serve_forever, daemon=True)
            console_thread.start()
            try:
                receipt.parent.mkdir(parents=True, exist_ok=True)
                ready = dict(pid=os.getpid(), url=console.url, save=str(save.resolve()),
                             started_utc=datetime.now(timezone.utc).isoformat())
                receipt.write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding='utf8')
                server.event({'event': 'listening', 'address': '127.0.0.1', 'port': server.server_address[1],
                              'save': str(save.resolve()), 'original_lobby_connected': False})
                yield server, console.url
            finally:
                console.shutdown()
                console_thread.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18897)
    parser.add_argument('--save', type=Path, default=NATIVE / 'local-save/profile.sqlite3')
    parser.add_argument('--log', type=Path, default=NATIVE / 'reports/local-backend.jsonl')
    parser.add_argument('--seconds', type=float, help='bounded integration-test lifetime')
    parser.add_argument('--console-port', type=int, default=18898)
    parser.add_argument('--receipt', type=Path, default=NATIVE / 'reports/local-console.json')
    parser.add_argument('--native-events', type=Path)
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error('invalid loopback port')
    try:
        with open_service(save=options.save, log=options.log, receipt=options.receipt,
                          native_events=options.native_events, port=options.port,
                          console_port=options.console_port) as (server, _):
            print('Local native-protocol endpoint: 127.0.0.1:' + str(server.server_address[1]), flush=True)
            timer = threading.Timer(options.seconds, server.shutdown) if options.seconds else None
            if timer:
                timer.daemon = True
                timer.start()
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
            finally:
                if timer:
                    timer.cancel()
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
