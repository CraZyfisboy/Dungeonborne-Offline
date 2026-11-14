"""Native single-player outcomes and original item protobuf settlement."""
from __future__ import annotations

import hashlib
import re
import struct
import time

from inventory import Storage, BACKPACK, POUCH, WORN


RUN_HEADER = b'DBRUN01\n'
TIMED_RUN_HEADER = b'DBRUN02\n'
RUN_LIMIT = 4*1024*1024
OUTCOMES = {1:'extracted',2:'dead',3:'aborted'}


class SettlementHandlers:
    def settle_local_run(self,run_id,data):
        """Apply a complete outcome once. A failed validation commits nothing."""
        header = data[:8]
        if not re.fullmatch(r'[0-9a-f]{32}',run_id) or not 43<=len(data)<=RUN_LIMIT or header not in (RUN_HEADER,TIMED_RUN_HEADER):
            raise ValueError('无效的地牢结算文件')
        if data[8:40]!=run_id.encode('ascii'):
            raise ValueError('地牢结算文件与本局编号不匹配')
        event,count=struct.unpack_from('<BH',data,40)
        outcome=OUTCOMES.get(event)
        if outcome is None or count>2048 or (outcome=='aborted' and count):
            raise ValueError('无效的地牢结算类型')
        fingerprint=hashlib.sha256(data).hexdigest()
        at=43
        alive_seconds=None
        if header==TIMED_RUN_HEADER:
            if len(data)<47:
                raise ValueError('地牢存活时间记录未写完')
            alive_seconds=struct.unpack_from('<I',data,43)[0]
            at=47
        entries=[]
        item_type=self.protocol.message_class('tgf.DItem')
        for _ in range(count):
            if at+13>len(data):
                raise ValueError('地牢物品列表未写完')
            kind,x,y,amount,length=struct.unpack_from('<BhhII',data,at)
            at+=13
            if kind not in (BACKPACK,WORN,POUCH) or not 0<length<=65536 or at+length>len(data):
                raise ValueError('地牢物品记录无效')
            item=item_type.FromString(data[at:at+length])
            at+=length
            self.inventory.dimensions(item.basic.type_id)
            if not 0<amount<=100000:
                raise ValueError('地牢物品数量无效')
            item.basic.count=amount
            entries.append((kind,x,y,item))
        if at!=len(data):
            raise ValueError('地牢结算文件尾部无效')
        notices=[]
        with self.store.transaction() as tx:
            prior=tx.db.execute('SELECT fingerprint,status FROM run_settlements WHERE run_id=?',(run_id,)).fetchone()
            if prior:
                if prior!=(fingerprint,outcome):
                    raise ValueError('本局已经写入另一份结算')
                return dict(run_id=run_id,status=outcome,replayed=True)
            record=tx.db.execute('SELECT status,role_guid,level_type,level_id,map_id,role_before,created FROM local_runs WHERE run_id=?',
                                 (run_id,)).fetchone()
            if not record or (record[0]!='active' and not (outcome=='aborted' and record[0]=='reserved')):
                raise ValueError('没有对应的活动地牢存档')
            _,guid,level_type,level_id,map_id,before_blob,created=record
            role=next((r for r in tx.profile.user_role if r.basic.role_guid==guid),None)
            if role is None:
                raise ValueError('找不到本局的本机角色')
            before=self.protocol.message_class('tgf.DBattleDeployRole').FromString(before_blob)
            if outcome=='dead':
                previous_pouch=role.deploy_role.small_backpack
                preserved=self.protocol.message('tgf.DRoleBackpackData')
                preserved.CopyFrom(previous_pouch)
                del preserved.items[:]
                self.store_outcome_items(tx,role,before,[(k,x,y,i) for k,x,y,i in entries if k==POUCH],
                                         pouch=preserved)
                starter=self.game_data.role(role.basic.type_id,guid,role.basic.role_name,tx.next_item_guid,
                                            role.basic.meta.gender,role.basic.meta.force,failed=True)
                role.deploy_role.wearing.CopyFrom(starter.deploy_role.wearing)
                role.deploy_role.backpack.CopyFrom(starter.deploy_role.backpack)
                role.deploy_role.backpack.row_size=self.inventory.bag_page_rows*tx.integer('backpack_pages')
                role.deploy_role.small_backpack.CopyFrom(preserved)
            elif outcome=='extracted':
                if not entries and any((before.wearing.items,before.backpack.items,before.small_backpack.items)):
                    raise ValueError('撤离时未读取到角色物品，原存档已保留')
                self.store_outcome_items(tx,role,before,entries)
            tx.db.execute('UPDATE local_runs SET status=? WHERE run_id=?',(outcome,run_id))
            tx.db.execute('INSERT INTO run_settlements VALUES(?,?,?,?,?)',
                          (run_id,fingerprint,outcome,count,int(time.time())))
            if outcome!='aborted':
                deploy=role.deploy_role
                old_level,old_exp=deploy.role_level,deploy.exp
                survived=alive_seconds if alive_seconds is not None else max(0,int(time.time())-created)
                experience=self.game_data.grant_role_experience(deploy,map_id,survived)
                self.game_data.initialize_lobby_links(tx.profile)
                result=self.make_settle_notice(tx,role,run_id,outcome,level_type,level_id,map_id,created,
                                               (old_level,old_exp,experience))
                notices.append(('battle_settle_sync',result))
                self.inventory_notice(tx,notices)
        for name,message in notices:
            self.queue_notice(name,message)
        return dict(run_id=run_id,status=outcome,items=count,replayed=False)

    def store_outcome_items(self,tx,role,before,entries,pouch=None):
        deploy=role.deploy_role
        guid=role.basic.role_guid
        current={item.basic.guid:item.basic.type_id for group in (before.backpack,before.wearing,before.small_backpack)
                 for item in group.items if 0<item.basic.guid<2**31}
        if pouch is None:
            targets={BACKPACK:deploy.backpack,WORN:deploy.wearing,POUCH:deploy.small_backpack}
        else:
            targets={POUCH:pouch}
        for dest in targets.values():
            del dest.items[:]
        used=set()
        for kind,x,y,item in entries:
            amount=item.basic.count
            width,height,stack=self.inventory.dimensions(item.basic.type_id)
            if amount>stack:
                raise ValueError('原版地牢物品堆叠超过配置')
            if item.basic.guid in used or current.get(item.basic.guid)!=item.basic.type_id:
                item.basic.guid=tx.next_item_guid()
            used.add(item.basic.guid)
            target=Storage(kind,guid,targets[kind],role)
            if kind==WORN:
                if x not in self.inventory.wear_slots_to_numbers or y!=0 or any(i.basic.position.x==x for i in target.entries()):
                    raise ValueError('原版地牢装备槽位无效或重叠')
            else:
                # The native grid may auto-place an item across a visual page
                # boundary. Retain the verified absolute position on extraction.
                if min(x,y)<0 or x+height>target.holder.row_size or y+width>target.holder.column_size:
                    raise ValueError('原版地牢物品超出背包尺寸')
                for other in target.entries():
                    ow,oh,_=self.inventory.dimensions(other.basic.type_id)
                    p=other.basic.position
                    if x<p.x+oh and p.x<x+height and y<p.y+ow and p.y<y+width:
                        raise ValueError('原版地牢物品位置重叠')
            self.inventory.position(item,target,x,y)
            target.append(item)

    def make_settle_notice(self,tx,role,run_id,outcome,level_type,level_id,map_id,created,progress):
        response=self.protocol.message('tgf.SCBattleSettleSync')
        result=response.result
        result.user_key.user_id=tx.profile.player_info.player.user_id
        result.user_key.zone_id=1
        result.role_guid=role.basic.role_guid
        result.room_key.battle_id=run_id
        result.map_data.map_id=map_id
        result.matching_id=run_id
        result.start_match_team_size=1
        ds=result.ds_result
        ds.ds_user_key=tx.profile.player_info.player.open_id
        ds.game_type=3
        ds.actor_type=3 if outcome=='extracted' else 2
        ds.is_login=True
        ds.batle_info.finish_scenes=2
        ds.player_statistics.map_id=map_id
        if outcome=='extracted':
            ds.player_backpack.CopyFrom(role.deploy_role.backpack)
            ds.player_wearing.CopyFrom(role.deploy_role.wearing)
        ds.player_small_backpack.CopyFrom(role.deploy_role.small_backpack)
        response.level_parameter.level_type=level_type
        response.level_parameter.level_id=level_id
        response.level_parameter.region='local'
        old_level,old_exp,experience=progress
        response.gs_result.old_level,response.gs_result.old_exp=old_level,old_exp
        response.gs_result.curr_level,response.gs_result.curr_exp=role.deploy_role.role_level,role.deploy_role.exp
        response.gs_result.exp_change_detail.exp_change=experience
        response.gs_result.exp_change_detail.add_exp=experience
        response.gs_result.settle_interface_type=1
        history=tx.profile.user_battle_data.battle_settlement_list
        row=history.add(battle_finish_timepoint=int(time.time()),battle_start_timepoint=created)
        row.room_key.CopyFrom(result.room_key)
        row.level_parameter.CopyFrom(response.level_parameter)
        row.gameover_result.CopyFrom(result)
        if len(history)>100:
            del history[:-100]
        return response
