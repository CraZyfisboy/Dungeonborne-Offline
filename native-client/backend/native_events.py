"""Game-owned local outcome and inventory-action spool, with fixed commands."""
from __future__ import annotations

import json
import re
import struct
import threading

from settlement import RUN_LIMIT


RUN_NAME = re.compile(r'outcome-([0-9a-f]{32})\.bin')
ACTION_NAME = re.compile(r'action-([1-9][0-9]{0,9})\.txt')


class NativeEvents:
    def __init__(self, directory, service, event):
        self.directory, self.service, self.event = directory, service, event
        self.done = set()
        self.refresh_number = 0
        self.published_revision = 0
        self.pending_updates = {}
        self.refresh_lock = threading.Lock()

    def refresh_inventory(self, updates=()):
        with self.refresh_lock:
            self.refresh_number += 1
            payload = b'DBGRID1\n' + struct.pack('<II', self.refresh_number, len(updates))
            payload += b''.join(struct.pack('<IBQhhHHBQhhI', *row) for row in updates)
            self.pending_updates[self.refresh_number] = payload
            self.publish_inventory_revision()

    def publish_inventory_revision(self):
        if self.refresh_number == self.published_revision:
            return
        temporary = self.directory / 'inventory-revision.tmp'
        try:
            for revision, payload in self.pending_updates.items():
                path = self.directory / ('inventory-update-%d.bin' % revision)
                pending = path.with_suffix('.tmp')
                pending.write_bytes(payload)
                pending.replace(path)
            temporary.write_text(str(self.refresh_number) + '\n', encoding='ascii', newline='\n')
            temporary.replace(self.directory / 'inventory-revision.txt')
        except OSError as exc:
            self.event({'event':'inventory_refresh_error','error':str(exc)})
        else:
            self.published_revision = self.refresh_number
            self.pending_updates.clear()

    def poll(self):
        # On Windows a concurrent read can briefly block replacing the marker.
        with self.refresh_lock:
            self.publish_inventory_revision()
        for path in self.directory.iterdir():
            run=RUN_NAME.fullmatch(path.name)
            action=ACTION_NAME.fullmatch(path.name)
            if not (run or action) or path.name in self.done or not path.is_file() or path.is_symlink():
                continue
            reply=path.with_suffix('.ack')
            try:
                with path.open('rb') as source:
                    data=source.read(RUN_LIMIT+1 if run else 129)
                if run:
                    result=self.service.settle_local_run(run[1],data)
                    ack='OK\n'
                else:
                    result=self.inventory_action(action[1],data)
                    ack='OK\n%d\n%d\n%d\n%d\n' % (result['pages'],result['next_cost'] or 0,result['gold'],result['warehouse_pages'])
                    if result['operation']=='backpack':
                        self.refresh_inventory()
                self.event({'event':'native_outcome' if run else 'native_inventory_action',**result})
            except Exception as exc:
                self.event({'event':'native_event_error','file':path.name,'error':str(exc)})
                ack='ERROR\n' + str(exc).replace('\r',' ').replace('\n',' ')[:160] + '\n'
            tmp=reply.with_suffix('.ack.tmp')
            try:
                tmp.write_text(ack,encoding='utf8',newline='\n')
                tmp.replace(reply)
            except OSError as exc:
                self.event({'event':'native_event_ack_error','file':path.name,'error':str(exc)})
                continue
            self.done.add(path.name)

    def inventory_action(self,number,data):
        if len(data)>128:
            raise ValueError('无效的本机库存操作')
        match=re.fullmatch(rb'DBACT01\n(status|warehouse|backpack)\n([0-9]{1,2})\n',data)
        if not match:
            raise ValueError('无效的本机库存操作')
        method=match[1].decode('ascii')
        # Scope idempotency to this single launcher-owned spool directory.
        request_id=self.directory.name+':'+number
        notices=[]
        with self.service.store.transaction() as tx:
            prior=tx.db.execute('SELECT payload,result FROM native_actions WHERE id=?',(request_id,)).fetchone()
            if prior:
                if prior[0]!=data:
                    raise ValueError('库存操作编号已被使用')
                return json.loads(prior[1])
            inventory=self.service.inventory
            if method=='warehouse':
                self.service.ensure_lobby_inventory(tx)
                inventory.add_page(tx)
            elif method=='backpack':
                inventory.upgrade_backpack(tx,int(match[2]))
            status=inventory.backpack_status(tx)
            result=dict(operation=method,pages=status['pages'],next_cost=status['next_cost'],gold=tx.coin().count,
                        warehouse_pages=len(tx.profile.user_invetory.inventory_pages))
            if method!='status':
                self.service.inventory_notice(tx,notices)
            tx.db.execute('INSERT INTO native_actions VALUES(?,?,?)',(request_id,data,json.dumps(result)))
        for name,message in notices:
            self.service.queue_notice(name,message)
        return result
