"""Original workshop materials, recipes, and heirlooms, persisted with the profile."""
from __future__ import annotations

from collections import Counter
import time

from inventory import BACKPACK, WAREHOUSE, WORN, AUTO_BACKPACK, AUTO_WAREHOUSE


class WorkshopHandlers:
    def initialize_workshop(self):
        self.dismantling = {r['item_id']:r['reward'] for r in self.game_data.tables['excel_gear_dismantle']}
        self.heirlooms = {r['id']:r for r in self.game_data.tables['excel_gear_heirloom_guide']}
        self.gem_guides = {r['id']:r for r in self.game_data.tables['excel_gear_gem_guide']}
        self.heirloom_items = {r['inst_id']:r for r in self.heirlooms.values()}
        self.crafting_clock = time.time

    def is_currency(self,type_id):
        return self.game_data.items.get(type_id,{}).get('item_major_type')=='EN_ITEM_MAJOR_TYPE_CURRENCY'

    def charge_materials(self,tx,costs,provided=(),quantity=1):
        required=Counter()
        for cost in costs:
            required[cost['type_id']]+=cost['count']*quantity
        spent=self.protocol.message('tgf.DItemGroup')
        chosen={p.guid:p for p in provided if p.guid>0}
        for type_id,amount in required.items():
            if self.is_currency(type_id):
                coin=tx.coin(type_id)
                if coin.count<amount:
                    raise ValueError('制作材料不足')
                coin.count-=amount
                spent.element.add().basic.CopyFrom(self.protocol.message('tgf.DItemBasic',type_id=type_id,count=amount))
                continue
            stocks=[i.basic.guid for i in self.inventory.items(tx.profile) if i.basic.type_id==type_id and i.basic.position.type in (WAREHOUSE,BACKPACK)]
            stocks.sort(key=lambda guid:(guid not in chosen,guid))
            for guid in stocks:
                if amount<=0:
                    break
                basic=self.protocol.message('tgf.DItemBasic',guid=guid,type_id=type_id)
                storage,item=self.inventory.locate(tx,basic)
                count=min(amount,item.basic.count)
                if guid in chosen and chosen[guid].count>0:
                    count=min(count,chosen[guid].count)
                used=spent.element.add()
                used.CopyFrom(item)
                used.basic.count=count
                storage.remove(guid)
                item.basic.count-=count
                if item.basic.count:
                    storage.append(item)
                    used.basic.guid=tx.next_item_guid()
                amount-=count
            if amount:
                raise ValueError('制作材料不足')
        return spent

    def reward_material(self,tx,type_id,count,storage=WAREHOUSE,guid=0):
        if self.is_currency(type_id):
            coin=tx.coin(type_id)
            if count<0 or coin.count+count>=2**31:
                raise ValueError('材料超过原版显示范围')
            coin.count+=count
            item=self.protocol.message('tgf.DItem')
            item.basic.type_id,item.basic.count=type_id,count
            return [item]
        return self.inventory.store_new_item(tx,type_id,count,storage,guid)

    @staticmethod
    def update_crafting_slot(slot, now):
        if not slot.uncompleted_count:
            return 0
        elapsed = max(0, now - slot.start_time)
        finished = min(slot.uncompleted_count, elapsed // slot.time_cost)
        slot.completed_count += finished
        slot.uncompleted_count -= finished
        slot.start_time += finished * slot.time_cost
        return finished

    def process_crafting(self):
        now = int(self.crafting_clock())
        with self.store.transaction() as tx:
            return [slot.recipe_id for slot in tx.profile.user_crafting_data.crafting
                    if self.update_crafting_slot(slot, now)]

    def rpc_crafting(self, tx, request, result, notifications):
        self.ensure_lobby_inventory(tx)
        crafting = tx.profile.user_crafting_data
        recipe = self.game_data.recipes.get(request.recipe_id)
        if recipe is None or request.recipe_id not in crafting.recipe:
            raise ValueError('炼金配方未解锁')
        if not 1 <= request.count <= recipe['count_limt']:
            raise ValueError('炼金数量超出配方上限')
        now = int(self.crafting_clock())
        slot = next((entry for entry in crafting.crafting if entry.recipe_id == request.recipe_id), None)
        if slot is None:
            slot = crafting.crafting.add(recipe_id=request.recipe_id, time_cost=recipe['time_cost'])
        else:
            self.update_crafting_slot(slot, now)
        if slot.completed_count + slot.uncompleted_count + request.count > recipe['count_max_lmit']:
            raise ValueError('炼金队列已满，请先领取成品')
        self.charge_materials(tx, recipe['item_cost'], request.cost_items, request.count)
        if not slot.uncompleted_count:
            slot.start_time = now
        slot.uncompleted_count += request.count
        result.data.CopyFrom(slot)
        self.inventory_notice(tx, notifications)

    def finish_crafting(self, tx, request, result, notifications, cancel):
        self.ensure_lobby_inventory(tx)
        slots = tx.profile.user_crafting_data.crafting
        recipe = self.game_data.recipes.get(request.recipe_id)
        index = next((i for i, slot in enumerate(slots) if slot.recipe_id == request.recipe_id), None)
        if index is None or recipe is None:
            raise ValueError('找不到该配方的炼金订单')
        storage = request.storage_type or WAREHOUSE
        if storage not in (WAREHOUSE, BACKPACK, AUTO_WAREHOUSE, AUTO_BACKPACK):
            raise ValueError('炼金物品存放位置无效')
        slot = slots[index]
        self.update_crafting_slot(slot, int(self.crafting_clock()))
        if cancel:
            quantity, rewards = slot.uncompleted_count, recipe['item_cost']
            if not quantity:
                raise ValueError('炼金已完成，请领取成品')
            slot.uncompleted_count = 0
        else:
            quantity, rewards = slot.completed_count, recipe['product']
            if not quantity:
                raise ValueError('炼金尚未完成')
            slot.completed_count = 0
        for reward in rewards:
            result.item.extend(self.reward_material(tx, reward['type_id'], reward['count'] * quantity, storage))
        if not slot.completed_count and not slot.uncompleted_count:
            del slots[index]
        result.recipe_id = request.recipe_id
        self.inventory_notice(tx, notifications)

    def rpc_crafting_cancel(self, tx, request, result, notifications):
        self.finish_crafting(tx, request, result, notifications, cancel=True)

    def rpc_crafting_recived(self, tx, request, result, notifications):
        self.finish_crafting(tx, request, result, notifications, cancel=False)

    def rpc_gear_dismantle(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        if not 1<=len(request.gear)<=512 or len({i.guid for i in request.gear})!=len(request.gear):
            raise ValueError('分解物品列表无效')
        rewards=Counter()
        for basic in request.gear:
            storage,item=self.inventory.locate(tx,basic)
            if storage.kind not in (WAREHOUSE,BACKPACK) or item.basic.type_id not in self.dismantling:
                raise ValueError('这件装备不能分解')
            count=basic.count or item.basic.count
            if not 1<=count<=item.basic.count or item.create_info.HasField('heirloom'):
                raise ValueError('分解数量或装备类型无效')
            storage.remove(item.basic.guid)
            item.basic.count-=count
            if item.basic.count:
                storage.append(item)
            for reward in self.dismantling[item.basic.type_id]:
                rewards[reward['type_id']]+=reward['count']*count
        for type_id,count in rewards.items():
            result.reward.extend(self.reward_material(tx,type_id,count))
        self.inventory_notice(tx,notifications)

    def rpc_market_sell_items(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        if not 1<=len(request.sold_item_basics)<=512 or len({i.guid for i in request.sold_item_basics})!=len(request.sold_item_basics):
            raise ValueError('出售物品列表无效')
        gain=0
        for basic in request.sold_item_basics:
            storage,item=self.inventory.locate(tx,basic)
            row=self.game_data.items.get(item.basic.type_id,{})
            count=basic.count or item.basic.count
            if storage.kind not in (WAREHOUSE,BACKPACK) or not 1<=count<=item.basic.count or row.get('system_repurchase_price',0)<=0:
                raise ValueError('商人不回收这件物品')
            storage.remove(item.basic.guid)
            item.basic.count-=count
            if item.basic.count:
                storage.append(item)
            gain+=row['system_repurchase_price']*count
        self.reward_material(tx,1001,gain)
        self.inventory_notice(tx,notifications)

    @staticmethod
    def workshop_cost(row,kind,chosen):
        options=row.get(kind,())
        pick=next((v for v in options if v['index']==(chosen or 1)),None)
        if pick is None:
            raise ValueError('原版材料方案不存在')
        return pick['items']

    @staticmethod
    def heirloom_data(tx):
        data=tx.profile.heirloom_data.data
        return data[0] if data else data.add()

    def heirloom_notice(self,tx,notifications,data):
        dirty=self.inventory_notice(tx,notifications)
        if not dirty.ListFields():
            notifications.append(('player_dirty_change',dirty))
        dirty.dirty_heirloom_data.data.add().CopyFrom(data)

    def rpc_gear_heirloom_unlock(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        row=self.heirlooms.get(request.heirloom_id)
        if row is None:
            raise ValueError('找不到原版传家宝')
        data=self.heirloom_data(tx)
        if request.heirloom_id not in data.unlock_heirloom_id:
            if not all(needed in data.unlock_heirloom_id for needed in row['unlock_condition']):
                raise ValueError('请先解锁上一阶传家宝')
            self.charge_materials(tx,self.workshop_cost(row,'unlock_costs',request.choose_cost_id),request.cost_item)
            data.unlock_heirloom_id.append(request.heirloom_id)
            self.heirloom_notice(tx,notifications,data)
        result.heirloom_id=request.heirloom_id

    def rpc_gear_gem_unlock(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        row=self.gem_guides.get(request.gem_id)
        if row is None:
            raise ValueError('找不到原版传家宝词条')
        data=self.heirloom_data(tx)
        if request.gem_id not in data.unlock_gem_id:
            self.charge_materials(tx,self.workshop_cost(row,'unlock_costs',request.choose_cost_id),request.cost_item)
            data.unlock_gem_id.append(request.gem_id)
            self.heirloom_notice(tx,notifications,data)
        result.gem_id=request.gem_id

    def rpc_gear_heirloom_instantiation(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        row=self.heirlooms.get(request.heirloom_id)
        data=self.heirloom_data(tx)
        if row is None or request.heirloom_id not in data.unlock_heirloom_id:
            raise ValueError('请先解锁该阶传家宝')
        goal=self.protocol.message('tgf.DItemPosition')
        goal.CopyFrom(request.position)
        if not goal.type:
            goal.type=AUTO_BACKPACK
        if goal.type in (BACKPACK,WORN,AUTO_BACKPACK) and not goal.guid:
            goal.guid=tx.profile.user_hall_setting.role_guid
        if goal.type not in (BACKPACK,WORN,AUTO_BACKPACK,WAREHOUSE,AUTO_WAREHOUSE):
            raise ValueError('传家宝存放位置无效')
        self.charge_materials(tx,self.workshop_cost(row,'instantiation_costs',request.choose_cost_id),request.cost_item)
        item=self.protocol.message('tgf.DItem')
        item.basic.type_id,item.basic.count,item.basic.guid=row['inst_id'],1,tx.next_item_guid()
        item.create_info.heirloom.owner.user_id=tx.profile.player_info.player.user_id
        item.create_info.heirloom.owner.zone_id=1
        item.create_info.create_timestamp.seconds=int(time.time())
        if row['group_id'] in data.heirloom_gem_data:
            item.data.gem_data.CopyFrom(data.heirloom_gem_data[row['group_id']])
        target,x,y=self.inventory.resolve_goal(tx,goal,item)
        conflicts=self.inventory.collisions(target,item,x,y)
        if conflicts:
            if target.kind!=WORN or len(conflicts)!=1:
                raise ValueError('打造位置已有物品')
            existing=self.inventory.copy(conflicts[0])
            target.remove(existing.basic.guid)
            if not existing.create_info.HasField('heirloom'):
                self.inventory.store_item(tx,existing,WAREHOUSE)
        self.inventory.position(item,target,x,y)
        target.append(item)
        result.heirloom_id=request.heirloom_id
        result.position.CopyFrom(item.basic.position)
        self.inventory_notice(tx,notifications)
