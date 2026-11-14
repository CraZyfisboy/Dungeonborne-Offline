"""Loopback-only player console for native saves and per-monster loot rules."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
from pathlib import Path
import secrets
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

from loot_rules import LootRules
from protocol import NATIVE


class PlayerConsole(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service):
        super().__init__(address, ConsoleRequest)
        self.service = service
        self.token = secrets.token_hex(24)
        self.rules = LootRules(service.game_data)
        self.origin = 'http://127.0.0.1:' + str(self.server_address[1])
        self.url = self.origin + '/?key=' + self.token

    def status(self):
        with self.service.store.transaction() as tx:
            return dict(gold=tx.coin().count, multiplier=tx.integer('drop_multiplier'),
                        warehouse_pages=len(tx.profile.user_invetory.inventory_pages),
                        roles=len(tx.profile.user_role), settings_apply='next_game_start')

    def catalog(self):
        rules = self.rules
        return dict(rarities=rules.rarities, items=list(rules.catalog.values()),
                    sources=[dict(id=i, name=r['name'], category=rules.category(r['name']), rank=rules.rank(i))
                             for i, r in sorted(rules.sources.items())],
                    difficulty_map=rules.presets['difficulty_map'])

    def update(self, operation, body):
        if not isinstance(body, dict):
            raise ValueError('请求内容无效')
        rules = self.rules
        if operation == 'rule':
            source = rules.integer(body.get('source'), 1, 2**31-1, '掉落来源')
            difficulty = rules.integer(body.get('difficulty'), 1, 3, '地图难度')
            expected = rules.integer(body.get('revision'), 0, 2**31-1, '配置版本')
            with self.service.store.transaction() as tx:
                revision = rules.save(tx, source, body.get('rules'), expected, difficulty)
                self.audit(tx, operation, dict(source=source,difficulty=difficulty,revision=revision,
                                               rules=rules.get(tx,source,difficulty)[1]))
            return dict(saved=True, revision=revision, settings_apply='next_game_start')
        if operation == 'multiplier':
            multiplier = rules.integer(body.get('multiplier'), 1, 10, '全局掉率倍率')
            with self.service.store.transaction() as tx:
                previous=tx.integer('drop_multiplier')
                tx.set_integer('drop_multiplier', multiplier)
                self.audit(tx, operation, dict(previous=previous,current=multiplier))
            return dict(saved=True, multiplier=multiplier, settings_apply='next_game_start')
        if operation == 'gold':
            amount = rules.integer(body.get('amount'), 1, 999999999, '增加金币数量')
            with self.service.store.transaction() as tx:
                coin = tx.coin()
                if coin.count + amount > 2147483647:
                    raise ValueError('金币总数超过原客户端整数显示范围')
                coin.count += amount
                self.audit(tx, operation, dict(amount=amount,previous=coin.count-amount,current=coin.count))
                result = dict(saved=True, gold=coin.count)
                dirty = self.service.protocol.message('tgf.SCPlayerDirtyChgSync')
                dirty.dirty_coin_data.add().CopyFrom(coin)
            self.service.queue_notice('player_dirty_change', dirty)
            return result
        raise ValueError('未知控制台操作')

    @staticmethod
    def audit(tx, operation, details):
        tx.db.execute('INSERT INTO console_events(utc,operation,details) VALUES(?,?,?)',
                      (datetime.now(timezone.utc).isoformat(), operation, json.dumps(details,ensure_ascii=False,sort_keys=True)))


class ConsoleRequest(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, data, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        parsed = urlparse(self.path)
        token = self.headers.get('X-Offline-Key') or parse_qs(parsed.query).get('key', [''])[0]
        if not secrets.compare_digest(token, self.server.token):
            self.send(403, dict(error='请从本地控制台启动入口打开'))
            return False
        if self.headers.get('Origin') not in (None, self.server.origin):
            self.send(403, dict(error='只接受本机控制台操作'))
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        assets = NATIVE / 'backend/console-ui'
        if path in ('/console.js', '/console.css'):
            name = path[1:]
            mime = 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8'
            self.send(200, (assets / name).read_bytes(), mime)
            return
        if not self.authorized():
            return
        try:
            if path == '/':
                text = (assets / 'index.html').read_text(encoding='utf8')
                self.send(200, text.replace('__LOCAL_KEY__', html.escape(self.server.token, quote=True)).encode('utf8'), 'text/html; charset=utf-8')
            elif path == '/api/status':
                self.send(200, self.server.status())
            elif path == '/api/catalog':
                self.send(200, self.server.catalog())
            elif path in ('/api/rule', '/api/default'):
                query = parse_qs(urlparse(self.path).query)
                source, difficulty = int(query['source'][0]), int(query['difficulty'][0])
                if path == '/api/default':
                    self.send(200, dict(rules=self.server.rules.default(source, difficulty)))
                else:
                    with self.server.service.store.transaction() as tx:
                        revision, rules = self.server.rules.get(tx, source, difficulty)
                    self.send(200, dict(revision=revision, rules=rules))
            else:
                self.send(404, dict(error='找不到这个控制台页面'))
        except (ValueError, KeyError, TypeError) as error:
            self.send(400, dict(error=str(error)))

    def do_POST(self):
        if not self.authorized():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024 * 1024 or self.headers.get_content_type() != 'application/json':
                raise ValueError('控制台请求格式或大小无效')
            body = json.loads(self.rfile.read(length))
            path = urlparse(self.path).path
            if not path.startswith('/api/'):
                raise ValueError('未知控制台操作')
            self.send(200, self.server.update(path[5:], body))
        except (ValueError, KeyError, TypeError) as error:
            self.send(400, dict(error=str(error)))
