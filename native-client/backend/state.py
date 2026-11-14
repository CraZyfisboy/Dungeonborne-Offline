"""Transactional native-protobuf save, without re-encoding item affixes as JSON."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
import threading


class SaveMismatch(RuntimeError):
    pass


def messages(root):
    yield root
    for field, value in root.ListFields():
        if field.message_type is None:
            continue
        if field.is_repeated:
            if field.message_type.GetOptions().map_entry:
                if field.message_type.fields_by_name['value'].message_type is None:
                    continue
                children = value.values()
            else:
                children = value
        else:
            children = (value,)
        for child in children:
            yield from messages(child)


def migrate_item_guids(tx):
    """Old test saves used int64 item IDs; the native lobby truncates to int32."""
    deploy = tx.protocol.message_class('tgf.DBattleDeployRole')
    runs = [(key, deploy.FromString(data)) for key, data in tx.db.execute(
        "SELECT run_id,role_before FROM local_runs WHERE status IN ('reserved','active')")]
    trees = [tx.profile, *(role for _, role in runs)]
    basics = [message for tree in trees for message in messages(tree)
              if message.DESCRIPTOR.full_name == 'tgf.DItemBasic']
    used = {m.guid for m in basics if 0 < m.guid < 2**31}
    moved = {}
    counter = 20000000
    for item in basics:
        if item.guid < 2**31:
            continue
        if item.guid not in moved:
            while counter in used:
                counter += 1
            if counter >= 2**31:
                raise SaveMismatch('native item identifier space exhausted during migration')
            moved[item.guid] = counter
            counter += 1
        item.guid = moved[item.guid]
    for tree in trees:
        for message in messages(tree):
            for field, value in message.ListFields():
                if field.message_type or field.is_repeated or not field.name.endswith('item_guid'):
                    continue
                if value in moved:
                    setattr(message, field.name, moved[value])
    while counter in used:
        counter += 1
    # Ensure future IDs cannot collide with any preserved, in-range IDs.
    counter = max(counter, max(used, default=0) + 1)
    tx.set_integer('next_item_guid', counter)
    for key, role in runs:
        tx.db.execute('UPDATE local_runs SET role_before=? WHERE run_id=?',
                      (role.SerializeToString(deterministic=True), key))
    tx.persist_profile()


def migrate_grid_axes(tx):
    """Earlier local saves packed columns in X; the original uses rows in X."""
    deploy = tx.protocol.message_class('tgf.DBattleDeployRole')
    runs = [(key, deploy.FromString(data)) for key, data in tx.db.execute(
        "SELECT run_id,role_before FROM local_runs WHERE status IN ('reserved','active')")]
    for tree in (tx.profile, *(role for _, role in runs)):
        for message in messages(tree):
            if message.DESCRIPTOR.full_name == 'tgf.DItemPosition' and message.type in (1,2,4):
                message.x, message.y = message.y, message.x
    for key, role in runs:
        tx.db.execute('UPDATE local_runs SET role_before=? WHERE run_id=?',
                      (role.SerializeToString(deterministic=True), key))
    tx.persist_profile()


def migrate_native_profile(tx, game_data):
    # Native warehouse contexts address their page array from zero.
    for index, page in enumerate(tx.profile.user_invetory.inventory_pages):
        page.page_id = index
        for group in page.item_group:
            for item in group.element:
                if item.basic.position.type == 1:
                    item.basic.position.guid = index
    for role in tx.profile.user_role:
        game_data.set_role_attributes(role.deploy_role)
    deploy = tx.protocol.message_class('tgf.DBattleDeployRole')
    runs = tx.db.execute("SELECT run_id,role_before FROM local_runs WHERE status IN ('reserved','active')").fetchall()
    for run_id, blob in runs:
        role = deploy.FromString(blob)
        game_data.set_role_attributes(role)
        tx.db.execute('UPDATE local_runs SET role_before=? WHERE run_id=?',
                      (role.SerializeToString(deterministic=True), run_id))
    game_data.initialize_lobby_links(tx.profile)
    tx.persist_profile()


class Transaction:
    def __init__(self, connection, protocol):
        self.db = connection
        self.protocol = protocol
        row = self.db.execute('SELECT data,data_sha256 FROM profile WHERE id=1').fetchone()
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise SaveMismatch('native profile checksum mismatch; existing save preserved')
        self.original = row[0]
        self.profile = protocol.message_class('tgf.SCPlayerGetInfoRsp').FromString(row[0])
        self.inventory_updates = []

    def integer(self, key):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        if row is None:
            raise SaveMismatch('missing native save metadata: ' + key)
        return int(row[0])

    def set_integer(self, key, value):
        self.db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, str(value)))

    def next_guid(self):
        guid = self.integer('next_guid')
        if guid >= 2**63 - 1:
            raise SaveMismatch('native role identifier range exhausted')
        self.set_integer('next_guid', guid + 1)
        return guid

    def next_item_guid(self):
        guid = self.integer('next_item_guid')
        if not 0 < guid < 2**31:
            raise SaveMismatch('original client item identifier range exhausted')
        self.set_integer('next_item_guid', guid + 1)
        return guid

    def coin(self, type_id=1001):
        for coin in self.profile.user_invetory.coins_data:
            if coin.coin_type_id == type_id:
                return coin
        return self.profile.user_invetory.coins_data.add(coin_type_id=type_id)

    def persist_profile(self):
        data = self.profile.SerializeToString(deterministic=True)
        if data != self.original:
            self.db.execute('UPDATE profile SET data=?, data_sha256=?, revision=revision+1 WHERE id=1',
                            (data, hashlib.sha256(data).hexdigest()))


class StateStore:
    def __init__(self, path, protocol, game_data):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protocol = protocol
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profile(
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                data BLOB NOT NULL, data_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS replies(
                session TEXT NOT NULL, sequence TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, frames BLOB NOT NULL,
                PRIMARY KEY(session,sequence));
            CREATE TABLE IF NOT EXISTS bills(
                bill_no TEXT PRIMARY KEY, method TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, response BLOB NOT NULL);
        ''')
        with self.lock:
            try:
                old, schema = self.read_schema()
                if old and schema[0] != '6':
                    self.backup_before_migration(schema[0])
                self.connection.execute('BEGIN IMMEDIATE')
                old, schema = self.read_schema()
                if old is None:
                    if self.connection.execute('SELECT 1 FROM profile').fetchone():
                        raise SaveMismatch('unrecognized native save; refusing to replace it')
                    data = game_data.profile().SerializeToString(deterministic=True)
                    self.connection.execute('INSERT INTO profile VALUES(1,0,?,?)', (data, hashlib.sha256(data).hexdigest()))
                    defaults = {'schema': '6', 'protocol_sha256': protocol.fingerprint,
                                'next_guid': '10000100000', 'next_item_guid': '20000000',
                                'drop_multiplier': '1', 'backpack_pages': '1'}
                    self.connection.executemany('INSERT INTO metadata VALUES(?,?)', defaults.items())
                self.connection.execute('''CREATE TABLE IF NOT EXISTS local_runs(
                    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, role_guid INTEGER NOT NULL,
                    level_type INTEGER NOT NULL, level_id INTEGER NOT NULL, map_id INTEGER NOT NULL,
                    map_path TEXT NOT NULL, role_before BLOB NOT NULL, created INTEGER NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS drop_rules(
                    source_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS difficulty_drop_rules(
                    source_id INTEGER NOT NULL, difficulty INTEGER NOT NULL CHECK(difficulty BETWEEN 1 AND 3),
                    revision INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(source_id,difficulty))''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS console_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, utc TEXT NOT NULL, operation TEXT NOT NULL,
                    details TEXT NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS trade_stock(
                    market INTEGER NOT NULL, product INTEGER NOT NULL,
                    cycle INTEGER NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(market,product))''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS trade_orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, market INTEGER NOT NULL, product INTEGER NOT NULL,
                    side TEXT NOT NULL, status TEXT NOT NULL, price INTEGER NOT NULL, quantity INTEGER NOT NULL,
                    items BLOB NOT NULL, published INTEGER NOT NULL, due INTEGER NOT NULL, hidden INTEGER NOT NULL DEFAULT 0)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS trade_claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER NOT NULL REFERENCES trade_orders(id),
                    type_id INTEGER NOT NULL, quantity INTEGER NOT NULL, item BLOB, received INTEGER NOT NULL DEFAULT 0)''')
                self.connection.execute('CREATE INDEX IF NOT EXISTS trade_claim_ticket ON trade_claims(ticket,received)')
                self.connection.execute("CREATE INDEX IF NOT EXISTS trade_due ON trade_orders(due) WHERE status='listed'")
                self.connection.execute('''CREATE TABLE IF NOT EXISTS run_settlements(
                    run_id TEXT PRIMARY KEY REFERENCES local_runs(run_id),
                    fingerprint TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER NOT NULL, finished INTEGER NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS native_actions(
                    id TEXT PRIMARY KEY, payload BLOB NOT NULL, result TEXT NOT NULL)''')
                if schema and schema[0] in ('1', '2', '3'):
                    migrate_item_guids(Transaction(self.connection, protocol))
                if schema and schema[0] in ('1', '2', '3', '4'):
                    migrate_grid_axes(Transaction(self.connection, protocol))
                if old and schema[0] != '6':
                    migrate_native_profile(Transaction(self.connection, protocol), game_data)
                    self.connection.execute("UPDATE metadata SET value='6' WHERE key='schema'")
                if self.connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise SaveMismatch('native save failed SQLite integrity check')
                self.connection.execute('COMMIT')
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute('ROLLBACK')
                self.connection.close()
                raise

    def read_schema(self):
        old = self.connection.execute("SELECT value FROM metadata WHERE key='protocol_sha256'").fetchone()
        if old and old[0] != self.protocol.fingerprint:
            raise SaveMismatch('client protocol differs from the existing native save')
        schema = self.connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if old and (not schema or schema[0] not in ('1', '2', '3', '4', '5', '6')):
            raise SaveMismatch('unsupported native save schema; refusing migration')
        return old, schema

    def backup_before_migration(self, schema):
        directory = self.path.parent / 'backups'
        directory.mkdir(exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup = directory / f'{self.path.stem}-{stamp}-schema{schema}.sqlite3'
        temporary = backup.with_suffix('.tmp')
        with closing(sqlite3.connect(temporary)) as snapshot:
            self.connection.backup(snapshot)
            if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise SaveMismatch('pre-migration backup failed SQLite integrity check')
        temporary.replace(backup)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                transaction = Transaction(self.connection, self.protocol)
                yield transaction
                transaction.persist_profile()
                self.connection.execute('COMMIT')
            except BaseException:
                self.connection.execute('ROLLBACK')
                raise

    def close(self):
        with self.lock:
            self.connection.close()

    def trade_due(self, now):
        with self.lock:
            return bool(self.connection.execute("SELECT 1 FROM trade_orders WHERE status='listed' AND due<=? LIMIT 1",(now,)).fetchone())
