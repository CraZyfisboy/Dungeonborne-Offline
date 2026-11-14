"""Virtual buyers and sellers behind the original trade RPCs."""
from __future__ import annotations

import hashlib
import random
import struct
import time

from affixes import TradeAffixes
from inventory import AUTO_BACKPACK, BACKPACK, WAREHOUSE
from protocol import NATIVE
from trade_armor import additional_trade_rows


AREA = 1
NPC_USER = 10000002
NPC_TICKET_BASE = 10_000_000_000
STOCK_SECONDS = 300
BUYER_DELAY = 20


class TradeHandlers:
    def initialize_trade(self, path=None):
        rows = self.game_data.tables['excel_trade_commodity'] + additional_trade_rows(self.game_data)
        self.trade_rows = {(r['market_id'],r['id']):r for r in rows
                           if r['is_on_shelf'] and r['price_def'] > 0}
        self.trade_ranges = [r for r in self.game_data.tables['excel_trade_valid_price'] if r['id'] == 1]
        self.trade_careers = {role['meta']['career'] for role in self.game_data.roles.values()}
        self.trade_templates = {}
        self.trade_affixes = TradeAffixes(self.game_data)
        self.trade_stock_fingerprint = None
        self.trade_clock = time.time
        self.trade_auto_delivery = True
        generated = NATIVE / 'local-assets/trade-stock.bin'
        paths = (path,) if path else ((generated,) if generated.is_file() else (
            NATIVE / 'config/trade-stock-v1.bin', NATIVE / 'config/trade-high-armor-stock.bin',
            NATIVE / 'config/trade-extra-equipment-stock.bin'))
        fingerprint = hashlib.sha256()
        legal = {r['item_id'] for r in self.trade_rows.values()}
        for stock in paths:
            if stock.is_file():
                fingerprint.update(self.load_trade_templates(stock, legal))
        if self.trade_templates:
            self.trade_stock_fingerprint = fingerprint.hexdigest()

    def load_trade_templates(self, path, legal):
        data = path.read_bytes()
        if not data.startswith(b'DBTRADE1') or len(data) > 4 * 1024 * 1024:
            raise ValueError('无效的原版交易行装备样本')
        at = 8
        while at < len(data):
            if len(data)-at < 8:
                raise ValueError('交易行装备样本未写完')
            type_id, size = struct.unpack_from('<II',data,at)
            at += 8
            if not 0 < size <= 65536 or at + size > len(data) or type_id not in legal or type_id in self.trade_templates:
                raise ValueError('无效的交易行装备记录')
            item = self.protocol.message_class('tgf.DItem').FromString(data[at:at+size])
            if item.basic.type_id != type_id or item.basic.count != 1:
                raise ValueError('交易行装备记录类型不匹配')
            item.basic.ClearField('guid')
            item.basic.ClearField('position')
            self.trade_templates[type_id] = item
            at += size
        return data

    def trade_stock_item(self, row):
        item = self.inventory.copy(self.trade_templates[row['item_id']])
        # A unique auction listing must display the same roll it delivers.
        rng = random.Random(row['id']) if row['market_id'] == 2 else random.SystemRandom()
        self.trade_affixes.roll(item, self.trade_rarity(row), rng)
        return item

    def trade_price(self, row):
        # TradeUtils.GetSpLimit floors standard price × configured percentage.
        # Homogeneous sales then choose the first legal price step on the
        # upper side of the lower bound (SetProgressBarSliderValue).
        low = max(1,row['price_def'] * row['price_lower_limit'] // 100)
        if row['market_id'] == 1:
            for band in self.trade_ranges:
                if band['sp_min'] <= low <= band['sp_max']:
                    step = band['range']
                    low = band['sp_min'] + (low - band['sp_min'] + step - 1)//step*step
                    break
        return low

    def trade_row(self, key, expected=None):
        row = self.trade_rows.get((key.trade_market_id,key.trade_product_id))
        if key.area_code not in (0,AREA) or not row or (expected is not None and key.trade_market_id != expected):
            raise ValueError('交易行没有这件原版商品')
        return row

    @staticmethod
    def trade_key(key,row):
        key.area_code, key.trade_market_id, key.trade_product_id = AREA,row['market_id'],row['id']

    def trade_stock(self,tx,row):
        if row['item_id'] not in self.trade_templates:
            return 0
        limit = 1 if row['market_id']==2 else max(8,row['purchase_count_limit'])
        cycle = int(self.trade_clock()) // STOCK_SECONDS
        entry = tx.db.execute('SELECT cycle,used FROM trade_stock WHERE market=? AND product=?',
                              (row['market_id'],row['id'])).fetchone()
        return max(0,limit-(entry[1] if entry and entry[0]==cycle else 0))

    def trade_consume_stock(self,tx,row,count):
        if not 1 <= count <= self.trade_stock(tx,row):
            raise ValueError('虚拟卖家的商品已卖完，稍后补货')
        cycle = int(self.trade_clock())//STOCK_SECONDS
        tx.db.execute('''INSERT INTO trade_stock(market,product,cycle,used) VALUES(?,?,?,?)
            ON CONFLICT(market,product) DO UPDATE SET
            used=CASE WHEN trade_stock.cycle=excluded.cycle THEN trade_stock.used+excluded.used ELSE excluded.used END,
            cycle=excluded.cycle''', (row['market_id'],row['id'],cycle,count))

    def trade_info(self,tx,out,row):
        self.trade_key(out.trade_product_key,row)
        out.standard_price = row['price_def']
        out.lowest_price = self.trade_price(row) if self.trade_stock(tx,row) else 0
        now = int(self.trade_clock())
        out.timestamp.seconds = now
        out.next_update_standard_price_time.seconds = (now//STOCK_SECONDS+1)*STOCK_SECONDS
        out.summary_version = now//STOCK_SECONDS

    def trade_rarity(self,row):
        item_id = row['item_id']
        item = self.game_data.items[item_id]
        source = self.game_data.sources.get(item.get('ue_source_type_id') or item_id,{})
        return self.game_data.enum('EnItemRarity',source.get('rarity','EN_ITEM_RARITY_NONE'))

    def trade_labels(self,row):
        type_id = row['item_id']
        item = self.game_data.items[type_id]
        gear = self.inventory.equipment(type_id)
        tags = {1005:{type_id}}
        if gear:
            tags[1001]={self.game_data.enum('EnGearEquipSubType',gear['equip_subtype'])}
            gear_careers = {career for cfg in gear.get('gear_configs', ())
                            for career in cfg['career_restrict'] or self.trade_careers}
            weapon_careers = {career for cfg in gear.get('weapon_configs', ()) for career in cfg['career_restrict']}
            tags[1002] = {self.game_data.enum('EnRoleCareerType', career) for career in gear_careers | weapon_careers}
            tags[1004]={x if isinstance(x,int) else self.game_data.enum('EnItemWearingPart',x)
                        for cfg in gear.get('gear_configs',()) for x in cfg['available_equip_slot']}
            for cfg in gear.get('weapon_configs',()):
                equip_type=cfg['weapon_equip_type']
                if not equip_type.endswith('ONLYLEFT'):
                    tags[1004].update((1,3))
                if not equip_type.endswith(('BOTHHAND','ONLYRIGHT')) or cfg['can_equip_double']:
                    tags[1004].update((2,4))
        major = self.game_data.enum('EnItemMajorType',item['item_major_type'])
        minor = self.game_data.enum('EnItemMinorType',item['item_minor_type'])
        return major,minor,tags,{1:self.trade_rarity(row),2:item.get('level_require',1),3:item.get('item_combat_value',0)}

    def trade_matches(self,row,rule):
        major,minor,tags,attributes=self.trade_labels(row)
        if rule.major_index_type not in (0,major) or rule.minor_index_type not in (0,minor):
            return False
        price=self.trade_price(row)
        if (rule.min_price>0 and price<rule.min_price) or (rule.max_price>0 and price>rule.max_price):
            return False
        if any(q.values and not (tags.get(q.type,set()) & set(q.values)) for q in rule.tags):
            return False
        if any(tags.get(q.type,set()) & set(q.values) for q in rule.exclude_tags):
            return False
        return all((q.type in attributes and (q.min_value<=0 or attributes[q.type]>=q.min_value)
                    and (q.max_value<=0 or attributes[q.type]<=q.max_value)) for q in rule.attributes)

    def trade_catalog(self,tx,market,rule=None,min_rarity=0,max_rarity=0,slots=(),subtypes=()):
        if market not in (1,2):
            raise ValueError('交易行分类无效')
        rows=[]
        for (market_id,_),row in self.trade_rows.items():
            if market_id!=market or not self.trade_stock(tx,row) or (rule is not None and not self.trade_matches(row,rule)):
                continue
            rarity=self.trade_rarity(row)
            if (min_rarity and rarity<min_rarity) or (max_rarity and rarity>max_rarity):
                continue
            if slots or subtypes:
                tags=self.trade_labels(row)[2]
                if slots and not set(slots)&tags.get(1004,set()) or subtypes and not set(subtypes)&tags.get(1001,set()):
                    continue
            rows.append(row)
        return sorted(rows,key=lambda r:(self.trade_price(r),r['id']))

    def trade_index_info(self,tx,out,row,index):
        self.trade_info(tx,out.basic_info,row)
        out.extra_info.trade_product_id=row['id']
        out.extra_info.sale_num=self.trade_stock(tx,row)
        out.extra_info.lowest_price=out.basic_info.lowest_price
        out.extra_info.rarity=self.trade_rarity(row)
        out.extra_info.search_order=index
        out.extra_info.has_order=True

    def rpc_trade_product_price_basic_info(self,tx,request,result,notifications):
        result.order_type=request.order_type
        for key in request.trade_product_key[:512]:
            self.trade_info(tx,result.info.add(),self.trade_row(key))

    def rpc_trade_product_price_info(self,tx,request,result,notifications):
        result.order_type=request.order_type
        row=self.trade_row(request.trade_product_key)
        self.trade_info(tx,result.info.basic_info,row)
        self.trade_key(result.info.trade_product_key,row)
        quantity=self.trade_stock(tx,row)
        price=self.trade_price(row)
        if quantity:
            result.info.on_sale.add(lowest_price=price,highest_price=price,order_ticket_count=1,item_count=quantity)
        point=result.info.metric_point.add(lowest_price=price,highest_price=price,standard_price=row['price_def'])
        point.timestamp.seconds=int(self.trade_clock())

    def rpc_trade_product_percentage_info(self,tx,request,result,notifications):
        row=self.trade_row(request.trade_product_key)
        self.trade_key(result.info.trade_product_key,row)

    def rpc_trade_product_recommend_order_ticket(self,tx,request,result,notifications):
        market=request.trade_market_id or 1
        out=result.result
        out.area_code,out.trade_market_id=AREA,market
        if request.HasField('hot_sale_rule'):
            rule=request.hot_sale_rule
            rows=self.trade_catalog(tx,market,rule.filter_rule.product_rule)
            limit=max(1,min(100,rule.count or 20))
            for idx,row in enumerate(rows[:limit],1):
                self.trade_index_info(tx,out.hot_sale_result.add().info,row,idx)
            out.hot_sale_more_info=len(rows)>limit
        for rule in ([request.rarity_wearing_part_rule] if request.HasField('rarity_wearing_part_rule') else [])+list(request.batch_rarity_wearing_part_rule)[:64]:
            limit=max(1,min(100,rule.count or 20))
            for slot in list(dict.fromkeys(rule.wearing_type))[:32] or [0]:
                rows=self.trade_catalog(tx,market,rule.filter_rule.product_rule,rule.min_rarity,rule.max_rarity,[slot] if slot else [])
                entry=out.rarity_wearing_part_result.add(min_rarity=rule.min_rarity,max_rarity=rule.max_rarity,wearing_type=slot,more_info=len(rows)>limit)
                for idx,row in enumerate(rows[:limit],1):
                    self.trade_index_info(tx,entry.info.add(),row,idx)
        for rule in request.batch_rarity_gear_sub_type_rule[:64]:
            limit=max(1,min(100,rule.count or 20))
            for subtype in list(dict.fromkeys(rule.gear_sub_type))[:32] or [0]:
                rows=self.trade_catalog(tx,market,rule.filter_rule.product_rule,rule.min_rarity,rule.max_rarity,subtypes=[subtype] if subtype else [])
                entry=out.rarity_gear_sub_type_result.add(min_rarity=rule.min_rarity,max_rarity=rule.max_rarity,gear_sub_type=subtype,more_info=len(rows)>limit)
                for idx,row in enumerate(rows[:limit],1):
                    self.trade_index_info(tx,entry.info.add(),row,idx)

    def rpc_trade_product_search_order_ticket(self,tx,request,result,notifications):
        market=request.trade_market_id or 1
        out=result.result
        out.area_code,out.trade_market_id=AREA,market
        rule=request.custom_search_rule
        rows=self.trade_catalog(tx,market,rule.filter_rule.product_rule)
        bound=rule.filter_rule.bound
        if bound.search_order>0:
            start=min(len(rows),bound.search_order)
        elif bound.trade_product_id:
            start=next((i+1 for i,row in enumerate(rows) if row['id']==bound.trade_product_id),0)
        else:
            start=0
        limit=max(1,min(100,rule.count or 30))
        for idx,row in enumerate(rows[start:start+limit],start+1):
            self.trade_index_info(tx,out.custom_search_result.add().info,row,idx)
        out.custom_search_more_info=len(rows)>start+limit

    def rpc_trade_career_rarity_affix_gear_set_price(self,tx,request,result,notifications):
        result.result.area_code=AREA
        result.result.trade_market_id=request.trade_market_id

    def trade_items(self,data):
        items = self.protocol.message_class('tgf.DItemGroup').FromString(data).element
        for item in items:
            self.trade_affixes.normalize_equipment(item)
        return items

    def read_claim_item(self, blob):
        item = self.protocol.message_class('tgf.DItem').FromString(blob)
        self.trade_affixes.normalize_equipment(item)
        return item

    def trade_new_order(self,tx,row,side,status,count,items,settles=0):
        group=self.protocol.message('tgf.DItemGroup')
        group.element.extend(items)
        return tx.db.execute('''INSERT INTO trade_orders(market,product,side,status,price,quantity,items,published,due)
                VALUES(?,?,?,?,?,?,?,?,?)''',(row['market_id'],row['id'],side,status,self.trade_price(row),count,
                                              group.SerializeToString(deterministic=True),int(self.trade_clock()),settles)).lastrowid

    def trade_new_claim(self,tx,ticket,item=None,coins=0):
        if item is not None:
            data=item.SerializeToString(deterministic=True)
            tx.db.execute('INSERT INTO trade_claims(ticket,type_id,quantity,item) VALUES(?,?,?,?)',
                          (ticket,item.basic.type_id,item.basic.count,data))
        elif coins > 0:
            tx.db.execute('INSERT INTO trade_claims(ticket,type_id,quantity) VALUES(?,1001,?)',(ticket,coins))

    def trade_advance(self,tx):
        now=int(self.trade_clock())
        completed=[]
        for ticket,market,product,price,count in tx.db.execute(
                "SELECT id,market,product,price,quantity FROM trade_orders WHERE status='listed' AND due<=?",(now,)).fetchall():
            row=self.trade_rows[(market,product)]
            gross=price*count
            proceeds=gross-gross*row['tax_percentage']//100
            self.trade_new_claim(tx,ticket,coins=proceeds)
            if self.trade_auto_delivery and tx.coin().count+proceeds<=2**31-1:
                tx.coin().count+=proceeds
                tx.db.execute('UPDATE trade_claims SET received=1 WHERE ticket=?',(ticket,))
            tx.db.execute("UPDATE trade_orders SET status='sold' WHERE id=?",(ticket,))
            completed.append(ticket)
        return completed

    def process_trade(self):
        if not self.store.trade_due(int(self.trade_clock())):
            return []
        notices=[]
        with self.store.transaction() as tx:
            completed=self.trade_advance(tx)
            if completed:
                self.trade_notice(tx,notices,completed)
        for name,body in notices:
            self.queue_notice(name,body)
        return completed

    def trade_order(self,tx,ticket):
        data=tx.db.execute('SELECT id,market,product,side,status,price,quantity,items,published,due FROM trade_orders WHERE id=?',(ticket,)).fetchone()
        if data is None:
            raise ValueError('找不到这张本机交易订单')
        return data

    def trade_meta(self,out,row,ticket,side,status,price,count,published,owner,due=0):
        self.trade_key(out.trade_product_key,row)
        out.trade_order_ticket_id=ticket
        out.label_cache_rarity=self.trade_rarity(row)
        out.ticket_state={'listed':12,'sold':21,'bought':21,'cancelled':23}[status]
        out.ticket_publish_timestamp.seconds=published
        out.valid_begin_timestamp.seconds=published
        out.valid_end_timestamp.seconds=max(published+3600,due)
        if due:
            out.next_confirmation_checkpoint.seconds=due
        if row['market_id']==2:
            auc=out.auction_order.auction_order_meta
            auc.auction_trade_order_ticket_id=ticket
            auc.origin_price=auc.current_price=auc.fixed_price=price
            auc.user_key.user_id,auc.user_key.zone_id=owner,AREA
            if side=='buy':
                bid=out.auction_order.bid_order_meta
                bid.bid_trade_order_ticket_id=ticket
                bid.price=price
                bid.user_key.user_id,bid.user_key.zone_id=owner,AREA
        elif side=='buy':
            order=out.purchase_order
            order.origin_item_count=count
            order.origin_price=order.current_unit_price=price
            order.is_fixed=True
            order.user_key.user_id,order.user_key.zone_id=owner,AREA
        else:
            order=out.sales_order
            order.origin_item_count=count
            order.left_item_count=count if status=='listed' else 0
            order.unit_price,order.unit_count=price,1
            order.user_key.user_id,order.user_key.zone_id=owner,AREA

    def trade_own_meta(self,tx,out,order):
        ticket,market,product,side,status,price,count,_,published,due=order
        row=self.trade_rows[(market,product)]
        self.trade_meta(out,row,ticket,side,status,price,count,published,tx.profile.player_info.player.user_id,due)
        if status in ('bought','sold') and tx.db.execute(
            'SELECT 1 FROM trade_claims WHERE ticket=? AND received=0 LIMIT 1',(ticket,)).fetchone():
            out.ticket_state=20

    def trade_confirmation(self,tx,out,order):
        ticket,_,_,side,status,price,count,_,_,_=order
        out.trade_order_ticket_id=ticket
        for claim,type_id,qty,blob in tx.db.execute(
                'SELECT id,type_id,quantity,item FROM trade_claims WHERE ticket=? AND received=0 ORDER BY id',(ticket,)):
            pending=out.pending_receive[claim]
            pending.trade_order_ticket_id=ticket
            out.receive_all_confirmation_key.append(claim)
            if status=='cancelled':
                pending.refund_data.confirmation_key=claim
                pending.refund_data.refund_items.add().CopyFrom(self.read_claim_item(blob))
                continue
            confirm=pending.confirmation_data.confirmation
            confirm.confirmation_key=claim
            confirm.confirmation_type=1
            confirm.confirm_timepoint.seconds=int(self.trade_clock())
            confirm.unit_price=price
            confirm.total_price=price*count
            confirm.final_price=qty if type_id==1001 else price*qty
            confirm.trade_count=count if type_id==1001 else qty
            if blob:
                confirm.item_data.CopyFrom(self.read_claim_item(blob))

    def trade_notice(self,tx,notifications,tickets):
        dirty=self.inventory_notice(tx,notifications)
        if not dirty.ListFields():
            notifications.append(('player_dirty_change',dirty))
        for ticket in sorted(set(tickets)):
            order=self.trade_order(tx,ticket)
            self.trade_own_meta(tx,dirty.dirty_trade_market.dirty_orders.add(),order)
            full=dirty.dirty_trade_market.dirty_confimation_full.add()
            self.trade_confirmation(tx,full,order)
            dirty.dirty_trade_market.dirty_confimation.extend(full.pending_receive.values())

    def trade_bill(self,tx,request,result,method):
        bill=getattr(request,'bill_no','')
        if len(bill)>128:
            raise ValueError('交易编号过长')
        fingerprint=hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
        if bill:
            prior=tx.db.execute('SELECT method,request_sha256,response FROM bills WHERE bill_no=?',(bill,)).fetchone()
            if prior:
                if prior[:2]!=(method,fingerprint):
                    raise ValueError('交易编号已用于其他内容')
                result.ParseFromString(prior[2])
                return None
        return bill,method,fingerprint

    @staticmethod
    def trade_save_bill(tx,bill,result):
        if bill and bill[0]:
            tx.db.execute('INSERT INTO bills VALUES(?,?,?,?)',(*bill,result.SerializeToString(deterministic=True)))

    def trade_buy(self,tx,row,count,offered):
        if not 1 <= count <= max(1,row['purchase_count_limit']):
            raise ValueError('购买数量超过原版订单上限')
        price=self.trade_price(row)
        if offered<price:
            raise ValueError('价格已变化，请刷新交易行')
        coin=tx.coin()
        if price*count>coin.count:
            raise ValueError('金币不足')
        self.trade_consume_stock(tx,row,count)
        items=[]
        stack=self.inventory.dimensions(row['item_id'])[2]
        for start in range(0,count,stack):
            item=self.trade_stock_item(row)
            item.basic.guid=tx.next_item_guid()
            item.basic.count=min(stack,count-start)
            items.append(item)
        ticket=self.trade_new_order(tx,row,'buy','bought',count,items)
        for item in items:
            self.trade_new_claim(tx,ticket,item)
            if self.trade_auto_delivery:
                self.inventory.store_item(tx,item,WAREHOUSE)
        if self.trade_auto_delivery:
            tx.db.execute('UPDATE trade_claims SET received=1 WHERE ticket=?',(ticket,))
        coin.count-=price*count
        return ticket

    def rpc_trade_purchase_order_publish(self,tx,request,result,notifications):
        bill=self.trade_bill(tx,request,result,'trade_purchase_order_publish')
        if bill is None:
            return
        self.ensure_lobby_inventory(tx)
        row=self.trade_row(request.trade_product_key,1)
        ticket=self.trade_buy(tx,row,request.count,request.price)
        result.trade_order_ticket_id=ticket
        result.is_fixed=request.is_fixed
        result.matchmaking_info.origin_price=request.price
        result.matchmaking_info.origin_count=request.count
        result.matchmaking_info.confirmation_price=self.trade_price(row)
        result.matchmaking_info.confirmation_count=request.count
        self.trade_notice(tx,notifications,[ticket])
        self.trade_save_bill(tx,bill,result)

    def trade_sell(self,tx,row,basics):
        if not basics or len(basics)>64 or len({b.guid for b in basics})!=len(basics):
            raise ValueError('上架物品无效')
        items=[]
        for basic in basics:
            storage,item=self.inventory.locate(tx,basic)
            count=basic.count or item.basic.count
            if storage.kind not in (WAREHOUSE,BACKPACK) or item.basic.type_id!=row['item_id'] or not 1<=count<=item.basic.count:
                raise ValueError('只有仓库或背包里可交易的物品能上架')
            if item.basic.expire_timepoint or item.data.HasField('insurance'):
                raise ValueError('限时或保险装备不能上架')
            storage.remove(item.basic.guid)
            if count<item.basic.count:
                remainder=self.inventory.copy(item)
                remainder.basic.count-=count
                storage.append(remainder)
                item.basic.guid=tx.next_item_guid()
            item.basic.count=count
            item.basic.ClearField('position')
            items.append(item)
        count=sum(i.basic.count for i in items)
        if count>max(1,row['sale_count_limit']):
            raise ValueError('上架数量超过原版订单上限')
        if tx.db.execute("SELECT COUNT(*) FROM trade_orders WHERE market=? AND status='listed'",(row['market_id'],)).fetchone()[0]>=30:
            raise ValueError('未成交的订单已满')
        return self.trade_new_order(tx,row,'sell','listed',count,items,int(self.trade_clock())+BUYER_DELAY)

    def rpc_trade_sales_order_publish(self,tx,request,result,notifications):
        bill=self.trade_bill(tx,request,result,'trade_sales_order_publish')
        if bill is None:
            return
        self.ensure_lobby_inventory(tx)
        completed=self.trade_advance(tx)
        ticket=self.trade_sell(tx,self.trade_row(request.trade_product_key,1),request.item_basics)
        result.trade_order_ticket_id=ticket
        self.trade_notice(tx,notifications,[ticket,*completed])
        self.trade_save_bill(tx,bill,result)

    def rpc_trade_auction_order_publish(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        completed=self.trade_advance(tx)
        ticket=self.trade_sell(tx,self.trade_row(request.trade_product_key,2),request.item_basics)
        result.trade_order_ticket_id=ticket
        self.trade_notice(tx,notifications,[ticket,*completed])

    def rpc_trade_bid_order_publish(self,tx,request,result,notifications):
        bill=self.trade_bill(tx,request,result,'trade_bid_order_publish')
        if bill is None:
            return
        self.ensure_lobby_inventory(tx)
        key=request.auction_ticket_key
        row=self.trade_row(key.trade_product_key,2)
        if key.trade_order_ticket_id!=NPC_TICKET_BASE+row['id']:
            raise ValueError('只能购买虚拟卖家当前上架的装备')
        ticket=self.trade_buy(tx,row,1,request.price)
        result.trade_order_ticket_id=ticket
        self.trade_own_meta(tx,result.auction_data,self.trade_order(tx,ticket))
        self.trade_notice(tx,notifications,[ticket])
        self.trade_save_bill(tx,bill,result)

    def rpc_trade_order_cancel(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        completed=self.trade_advance(tx)
        order=self.trade_order(tx,request.trade_order_ticket_id)
        if order[4]=='cancelled':
            return
        if order[4]!='listed':
            raise ValueError('这张订单已经成交')
        for item in self.trade_items(order[7]):
            self.trade_new_claim(tx,order[0],item)
            if self.trade_auto_delivery:
                self.inventory.store_item(tx,item,WAREHOUSE)
        if self.trade_auto_delivery:
            tx.db.execute('UPDATE trade_claims SET received=1 WHERE ticket=?',(order[0],))
        tx.db.execute("UPDATE trade_orders SET status='cancelled' WHERE id=?",(order[0],))
        self.trade_notice(tx,notifications,[order[0],*completed])

    def rpc_trade_order_history(self,tx,request,result,notifications):
        completed=self.trade_advance(tx)
        if completed:
            self.trade_notice(tx,notifications,completed)
        tickets=tx.db.execute('''SELECT id FROM trade_orders WHERE hidden=0 AND (market=? OR ?=0)
                    ORDER BY EXISTS(SELECT 1 FROM trade_claims WHERE ticket=trade_orders.id AND received=0) DESC,id DESC LIMIT 100''',
                    (request.market_id,request.market_id))
        for (ticket,) in tickets:
            order=self.trade_order(tx,ticket)
            out=result.meta_confirmation.add()
            self.trade_own_meta(tx,out.meta,order)
            self.trade_confirmation(tx,out.confirmation,order)

    def rpc_trade_order_delete(self,tx,request,result,notifications):
        dirty=self.protocol.message('tgf.SCPlayerDirtyChgSync')
        for ticket in request.trade_order_ticket_id[:100]:
            order=self.trade_order(tx,ticket)
            if order[4]=='listed' or tx.db.execute('SELECT 1 FROM trade_claims WHERE ticket=? AND received=0',(ticket,)).fetchone():
                raise ValueError('请先取消订单或领取成交物品')
            tx.db.execute('UPDATE trade_orders SET hidden=1 WHERE id=?',(ticket,))
            dirty.dirty_trade_market.delete_orders.append(ticket)
        if dirty.ListFields():
            notifications.append(('player_dirty_change',dirty))

    def rpc_trade_receive_confirmation_product(self,tx,request,result,notifications):
        self.ensure_lobby_inventory(tx)
        completed=self.trade_advance(tx)
        if not 1<=len(request.receive_confirmation_product)<=512:
            raise ValueError('待领取订单数量无效')
        tickets=[]
        for wanted in request.receive_confirmation_product:
            row=tx.db.execute('SELECT ticket,type_id,quantity,item,received FROM trade_claims WHERE id=?',
                              (wanted.confirmation_id,)).fetchone()
            if not row:
                raise ValueError('找不到待领取的交易商品')
            ticket,type_id,qty,blob,received=row
            if received:
                continue
            item = self.read_claim_item(blob) if blob else None
            current_type = item.basic.type_id if item is not None else type_id
            if wanted.type_id not in (0,type_id,current_type) or wanted.count not in (0,qty):
                raise ValueError('待领取的商品数量变化，请刷新交易行')
            if item is not None:
                goal=wanted.goal_position
                if goal.type in (BACKPACK,AUTO_BACKPACK):
                    self.inventory.store_item(tx,item,goal.type,goal.guid)
                elif goal.type in (0,WAREHOUSE,101):
                    self.inventory.store_item(tx,item,WAREHOUSE,goal.guid)
                else:
                    raise ValueError('交易物品只能放入仓库或背包')
            elif type_id==1001:
                coin=tx.coin()
                if coin.count+qty>2**31-1:
                    raise ValueError('金币超过原版显示范围')
                coin.count+=qty
            else:
                raise ValueError('未知的交易收益类型')
            tx.db.execute('UPDATE trade_claims SET received=1 WHERE id=?',(wanted.confirmation_id,))
            tickets.append(ticket)
        if tickets or completed:
            self.trade_notice(tx,notifications,[*tickets,*completed])

    def trade_npc_meta(self,out,row):
        now=int(self.trade_clock())
        self.trade_meta(out,row,NPC_TICKET_BASE+row['id'],'sell','listed',self.trade_price(row),1,now,NPC_USER)

    def rpc_trade_auction_index(self,tx,request,result,notifications):
        rule=request.search_rule
        rows=self.trade_catalog(tx,rule.trade_market_id or 2,rule)
        start=next((i+1 for i,r in enumerate(rows) if NPC_TICKET_BASE+r['id']==rule.bound_exclude.trade_order_ticket_id),0)
        for row in rows[start:start+32]:
            self.trade_npc_meta(result.orders.add(),row)
        result.next_page=len(rows)>start+32

    def rpc_trade_auction_recommend_order_ticket(self,tx,request,result,notifications):
        market=request.trade_market_id or 2
        out=result.result
        out.area_code,out.trade_market_id=AREA,market
        def fill(rule,entry):
            rows=self.trade_catalog(tx,market,rule.filter_rule)
            limit=max(1,min(64,rule.count or 20))
            for row in rows[:limit]:
                self.trade_npc_meta(entry.order_tickets.add(),row)
            entry.has_more=len(rows)>limit
        for name in ('sp_score','publish_new','best_selling_amount','best_selling_price_increase'):
            if request.HasField(name+'_rule'):
                fill(getattr(request,name+'_rule'),getattr(out,name+'_result'))
        for batch in request.batch_rule[:64]:
            entry=out.batch_result.add(sequence=batch.sequence)
            if batch.HasField('private_data'):
                entry.private_data.CopyFrom(batch.private_data)
            fill(batch.rule,entry.result)
        if request.HasField('rarity_wearing_part_rule'):
            rule=request.rarity_wearing_part_rule
            for slot in list(rule.wearing_parts) or [0]:
                rows=self.trade_catalog(tx,market,rule.filter_rule,rule.min_rarity,rule.max_rarity,[slot] if slot else [])
                limit=max(1,min(64,rule.count or 20))
                entry=out.rarity_wearing_part_result.add(min_rarity=rule.min_rarity,max_rarity=rule.max_rarity,wearing_part=slot,has_more=len(rows)>limit)
                for row in rows[:limit]:
                    self.trade_npc_meta(entry.order_tickets.add(),row)

    def rpc_trade_auction_fetch_order_info(self,tx,request,result,notifications):
        completed=self.trade_advance(tx)
        if completed:
            self.trade_notice(tx,notifications,completed)
        for key in request.auction_ticket_key[:64]:
            row=self.trade_row(key.trade_product_key,2)
            if key.trade_order_ticket_id==NPC_TICKET_BASE+row['id'] and self.trade_stock(tx,row):
                detail=result.auction_order_info.add().order
                self.trade_npc_meta(detail.meta,row)
                detail.item_data.CopyFrom(self.trade_stock_item(row))
                detail.publisher.user_id,detail.publisher.zone_id=NPC_USER,AREA
            else:
                order=self.trade_order(tx,key.trade_order_ticket_id)
                if order[1:3]!=(row['market_id'],row['id']):
                    raise ValueError('交易行订单与商品不匹配')
                detail=result.auction_order_info.add().order
                self.trade_own_meta(tx,detail.meta,order)
                detail.item_data.CopyFrom(self.trade_items(order[7])[0])
                detail.publisher.user_id,detail.publisher.zone_id=tx.profile.player_info.player.user_id,AREA
            major,minor,tags,attributes=self.trade_labels(row)
            detail.label.major_index_type,detail.label.minor_index_type=major,minor
            for kind,values in tags.items():
                detail.label.tags.add(type=kind,values=sorted(values))
            for kind,value in attributes.items():
                detail.label.attributes.add(type=kind,values=value)
            detail.product_profile.price_min=self.trade_price(row)
            detail.product_profile.price_max=row['price_def']*row['price_upper_limit']//100
