"""Original CSMsg business handlers. Unknown RPCs fail instead of faking success."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import secrets
import struct
import time
import threading
from google.protobuf import json_format
from inventory import Inventory, BACKPACK, WAREHOUSE, WORN, POUCH, AUTO_WAREHOUSE, AUTO_BACKPACK
from game_data import ADVANCED_POTION_PRODUCTS
from market import TradeHandlers, NPC_USER
from settlement import SettlementHandlers
from workshop import WorkshopHandlers


class RpcError(Exception):
    def __init__(self, message, code=-1):
        super().__init__(message)
        self.code = code


@dataclass
class Session:
    key: str = field(default_factory=lambda: secrets.token_hex(16))
    logged_in: bool = False
    sequence: int = 0


def pack_frames(frames):
    return b''.join(struct.pack('!I', len(frame)) + frame for frame in frames)


def unpack_frames(data):
    frames = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise ValueError('truncated local frame header')
        length = struct.unpack_from('!I', data, offset)[0]
        offset += 4
        if not 0 < length <= 4 * 1024 * 1024 or offset + length > len(data):
            raise ValueError('invalid local frame size')
        frames.append(data[offset:offset+length])
        offset += length
    return frames


class LocalService(TradeHandlers, SettlementHandlers, WorkshopHandlers):
    def __init__(self, protocol, store, game_data):
        self.protocol, self.store, self.game_data = protocol, store, game_data
        self.inventory = Inventory(protocol, game_data)
        self.notice_lock = threading.Lock()
        self.pending_notices = []
        self.initialize_trade()
        self.initialize_workshop()

    def queue_notice(self, name, message):
        with self.notice_lock:
            self.pending_notices.append((name, message.SerializeToString(deterministic=True)))

    def drain_notices(self, session):
        if not session.logged_in:
            return []
        with self.notice_lock:
            notices, self.pending_notices = self.pending_notices, []
        result = []
        for name, body in notices:
            rpc = self.protocol.client_rpc(name)
            message = self.protocol.message_class(rpc.output).FromString(body)
            result.append(self.notification(session, name, message))
        return result

    def envelope(self, session, request, rpc, body, error=0, detail=''):
        result = self.protocol.message('atframework.CSMsg')
        result.head.client_sequence = request.head.client_sequence
        session.sequence += 1
        result.head.server_sequence = session.sequence
        result.head.timestamp = int(time.time())
        result.head.error_code = error
        result.head.external_error_message = detail
        result.head.op_type = 3
        result.head.rpc_response.version = request.head.rpc_request.version or '0.0.0'
        result.head.rpc_response.rpc_name = 'tgf.GameClientService.' + rpc.name
        result.head.rpc_response.type_url = rpc.output
        result.body_bin = body.SerializeToString(deterministic=True)
        return result.SerializeToString(deterministic=True)

    def notification(self, session, name, body):
        rpc = self.protocol.client_rpc(name)
        result = self.protocol.message('atframework.CSMsg')
        session.sequence += 1
        result.head.server_sequence = session.sequence
        result.head.timestamp = int(time.time())
        result.head.op_type = 1
        result.head.rpc_stream.version = '0.0.0'
        result.head.rpc_stream.rpc_name = 'tgf.GameClientService.' + name
        result.head.rpc_stream.type_url = rpc.output
        result.body_bin = body.SerializeToString(deterministic=True)
        return result.SerializeToString(deterministic=True)

    def dispatch(self, packet, session):
        request = self.protocol.message_class('atframework.CSMsg').FromString(packet)
        method = request.head.rpc_request.rpc_name
        prefix = 'tgf.GameClientService.'
        if not request.HasField('head') or not request.head.HasField('rpc_request') or not method.startswith(prefix):
            raise ValueError('not an original client business request')
        rpc = self.protocol.client_rpc(method[len(prefix):])
        if request.head.rpc_request.type_url not in ('', rpc.input):
            raise ValueError('original client request type mismatch')
        body = self.protocol.message_class(rpc.input).FromString(request.body_bin)
        if rpc.name == 'login' and session.logged_in:
            # Returning through the original login world can restart client
            # request sequences without replacing the adapter's TCP socket.
            # Keep server sequence monotonic, but start a fresh reply ledger.
            session.key = secrets.token_hex(16)
        output = self.protocol.message(rpc.output)
        fingerprint = hashlib.sha256(packet).hexdigest()
        sequence = str(request.head.client_sequence)
        fault = None
        with self.store.transaction() as transaction:
            prior = transaction.db.execute('SELECT request_sha256,frames FROM replies WHERE session=? AND sequence=?',
                                           (session.key, sequence)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError('client reused a request sequence for different content')
                return unpack_frames(prior[1]), {'rpc': rpc.name, 'replayed': True, 'error': 0}
            transaction.db.execute('SAVEPOINT game_action')
            notifications = []
            try:
                if not session.logged_in and rpc.name not in ('login', 'pre_login', 'ping', 'business_ping'):
                    raise RpcError('local account has not logged in')
                handler = getattr(self, 'rpc_' + rpc.name, None)
                if handler is None:
                    raise RpcError('local handler pending: ' + rpc.name)
                handler(transaction, body, output, notifications)
                error, detail = 0, ''
                transaction.db.execute('RELEASE game_action')
            except (RpcError, ValueError, TypeError, AttributeError, KeyError) as exc:
                transaction.db.execute('ROLLBACK TO game_action')
                transaction.db.execute('RELEASE game_action')
                transaction.profile.ParseFromString(transaction.original)
                output.Clear()
                notifications = []
                if isinstance(exc,(RpcError,ValueError)):
                    error, detail = getattr(exc, 'code', -1), str(exc)
                else:
                    fault = type(exc).__name__ + ': ' + str(exc)
                    error, detail = -1, '本机服务未能处理这次操作，存档未更改'
            frames = [self.envelope(session, request, rpc, output, error, detail)]
            frames.extend(message if name == '_native_action' else self.notification(session, name, message)
                          for name, message in notifications)
            transaction.db.execute('INSERT INTO replies VALUES(?,?,?,?)', (session.key, sequence, fingerprint, pack_frames(frames)))
            inventory_updates = transaction.inventory_updates if not error else []
        if rpc.name == 'login' and not error:
            session.logged_in = True
        outcome={'rpc': rpc.name, 'replayed': False, 'error': error, 'detail': detail}
        if fault:
            outcome['handler_fault']=fault
        if inventory_updates:
            outcome['inventory_updates'] = inventory_updates
        if any(name == 'player_dirty_change' and message.dirty_coin_data for name, message in notifications):
            outcome['coin_changed'] = True
        return frames, outcome

    def rpc_pre_login(self, tx, request, result, notifications):
        result.info.allow_login = True

    def rpc_login(self, tx, request, result, notifications):
        self.game_data.initialize_lobby_links(tx.profile)
        for item in self.inventory.items(tx.profile):
            self.trade_affixes.normalize_equipment(item)
        tx.profile.trade_market_logic_data.is_trade_unlock = True
        for tab in sorted(self.game_data.merchant_tabs):
            self.merchant_tab(tx, tab)
        result.open_id = tx.profile.player_info.player.open_id
        result.user_id = tx.profile.player_info.player.user_id
        result.account_id = result.user_id
        result.zone_id = 1
        result.reconnect_uid = result.user_id
        result.heartbeat_interval = 15
        result.config_cs_task_timeout = 30
        result.version = request.package_version or '1.0.0.97'
        result.is_new_player = False

    def rpc_ping(self, tx, request, result, notifications):
        pass

    rpc_business_ping = rpc_ping
    rpc_client_action_log = rpc_ping
    rpc_network_latency_report = rpc_ping
    rpc_network_block_loading = rpc_ping
    rpc_telemetry_report = rpc_ping
    rpc_player_ace_packet = rpc_ping

    def rpc_player_get_info(self, tx, request, result, notifications):
        if request.need_user_market_tabs:
            for tab in sorted(self.game_data.merchant_tabs):
                self.merchant_tab(tx, tab)
        flags = {field.name for field, value in request.ListFields() if value}
        if not flags or 'need_user_crafting' in flags:
            now = int(self.crafting_clock())
            for slot in tx.profile.user_crafting_data.crafting:
                self.update_crafting_slot(slot, now)
        if not flags:
            result.CopyFrom(tx.profile)
            for index in range(len(result.user_market_tabs)-1,-1,-1):
                if result.user_market_tabs[index].market_tab_id not in self.game_data.merchant_tabs:
                    del result.user_market_tabs[index]
            return
        fields = {
            'need_user_sold_items': 'user_sold_items_list', 'need_user_crafting': 'user_crafting_data',
            'need_server_zone_offset': 'zone_offset', 'need_user_soul_bound': 'user_soul_bound_data',
            'need_user_activity_center': 'activity_center_data', 'need_user_private_room': 'private_room_data',
            'need_user_facade': 'facade_data', 'need_user_client_cfg': 'rainbow_client',
            'need_user_heirloom_data': 'heirloom_data', 'need_user_adult': 'adult_data',
            'need_mithril_force': 'mithril_force_data', 'need_trade_market': 'trade_market_logic_data',
            'need_market_video_popup': 'popup_videos',
        }
        for flag in flags:
            name = fields.get(flag, flag.removeprefix('need_'))
            field = result.DESCRIPTOR.fields_by_name.get(name)
            if field is None:
                continue
            source = getattr(tx.profile, name)
            if field.is_repeated:
                getattr(result, name).extend(
                    (tab for tab in source if tab.market_tab_id in self.game_data.merchant_tabs)
                    if name=='user_market_tabs' else source)
            elif field.message_type:
                getattr(result, name).CopyFrom(source)
            else:
                setattr(result, name, source)

    def rpc_role_fetch_user_role(self, tx, request, result, notifications):
        for role in tx.profile.user_role:
            if role.basic.role_guid == request.role_id:
                result.role.CopyFrom(role)
                return
        raise RpcError('local role not found')

    def rpc_battle_fetch_battle_user_data(self, tx, request, result, notifications):
        role = next((r for r in tx.profile.user_role
                     if r.basic.role_guid == tx.profile.user_hall_setting.role_guid), None)
        if role is None:
            raise RpcError('请先在营地选择角色')
        data = result.data
        data.user_key.user_id = tx.profile.player_info.player.user_id
        data.user_key.zone_id = 1
        data.user_parameter.team_id = tx.profile.user_team.current_team.team_key.team_id
        data.user_parameter.adjust_team_size = 1
        data.user_parameter.start_match_team_size = 1
        data.deploy_preview.deploy_role.CopyFrom(role.deploy_role)
        active = tx.db.execute("SELECT role_before FROM local_runs WHERE status IN ('reserved','active') ORDER BY created DESC LIMIT 1").fetchone()
        if active:
            data.deploy_preview.deploy_role.ParseFromString(active[0])
            tx.db.execute("UPDATE local_runs SET status='active' WHERE status='reserved'")
        data.deploy_preview.client_setting_for_ds.CopyFrom(tx.profile.player_info.player_setting)
        data.ds_player_key.ds_user_key = tx.profile.player_info.player.open_id
        data.ds_player_key.ds_token = 'local-single-player'

    def rpc_battle_matching_pre_search(self, tx, request, result, notifications):
        self.game_data.dungeon(request.level_select)
        if not any(r.basic.role_guid == tx.profile.user_hall_setting.role_guid for r in tx.profile.user_role):
            raise RpcError('请先在营地选择角色')
        # A local player has no matchmaking wait or remote region selection.
        result.lazy_search_random_begin_second = 0
        result.lazy_search_random_end_second = 0

    rpc_battle_matching_cancel_pre_search = rpc_ping

    def rpc_battle_matching_search(self, tx, request, result, notifications):
        detail, scene, competition = self.game_data.dungeon(request.level_select)
        role = next((r for r in tx.profile.user_role if r.basic.role_guid == tx.profile.user_hall_setting.role_guid), None)
        if role is None:
            raise RpcError('请先在营地选择角色')
        active = tx.db.execute("SELECT run_id,role_guid,level_type,level_id FROM local_runs WHERE status IN ('reserved','active') ORDER BY created DESC LIMIT 1").fetchone()
        if active:
            run_id, guid, level_type, level_id = active
            if (guid, level_type, level_id) != (role.basic.role_guid, request.level_select.level_type, request.level_select.level_id):
                raise RpcError('请先结束当前本地地牢')
        else:
            run_id = secrets.token_hex(16)
            tx.db.execute('INSERT INTO local_runs VALUES(?,?,?,?,?,?,?,?,?)',
                          (run_id, 'reserved', role.basic.role_guid, request.level_select.level_type,
                           request.level_select.level_id, detail['map_id'], scene,
                           role.deploy_role.SerializeToString(deterministic=True), int(time.time())))
        result.matching_id = run_id
        result.matching_heartbeat_interval = 15
        result.battle_heartbeat_interval = 15
        result.level_parameter.level_type = request.level_select.level_type
        result.level_parameter.level_id = request.level_select.level_id
        result.level_parameter.matching_pool_id = competition['detail']['match_making_pool_id']
        result.level_parameter.region = request.level_select.region
        entry = next((r['ue_path'] for r in detail['map_config']
                      if r['room_type'] == 'EN_DS_BATTLE_SCENES_TYPE_WAITING_ROOM'), scene)
        action = ('DBACTION1\nstart_battle\n%s\n%s\n%d\n%d\n%d\n%s\n' %
                  (run_id, scene, detail['map_id'], detail['timeout'], tx.integer('drop_multiplier'), entry)).encode('ascii')
        notifications.append(('_native_action', action))

    def rpc_battle_matching_heartbeat(self, tx, request, result, notifications):
        if not tx.db.execute("SELECT 1 FROM local_runs WHERE run_id=? AND status IN ('reserved','active')", (request.matching_id,)).fetchone():
            raise RpcError('找不到当前本地地牢')

    def rpc_battle_matching_exit(self, tx, request, result, notifications):
        # Once the saved character has entered, leaving matchmaking must not
        # erase its active journal; death/extraction performs settlement later.
        tx.db.execute("UPDATE local_runs SET status='cancelled' WHERE run_id=? AND status='reserved'", (request.matching_id,))

    rpc_battle_matching_confirm_refuse = rpc_battle_matching_exit

    def ensure_lobby_inventory(self, tx):
        if tx.db.execute("SELECT 1 FROM local_runs WHERE status IN ('reserved','active') LIMIT 1").fetchone():
            raise RpcError('请先结束当前地牢，再整理营地物品')

    def inventory_notice(self, tx, notifications):
        old = self.protocol.message_class('tgf.SCPlayerGetInfoRsp').FromString(tx.original)
        dirty = self.protocol.message('tgf.SCPlayerDirtyChgSync')
        previous = {i.basic.guid:i for i in self.inventory.items(old)}
        current = {i.basic.guid:i for i in self.inventory.items(tx.profile)}
        tx.inventory_updates = []
        for guid in previous.keys() | current.keys():
            before, after = previous.get(guid), current.get(guid)
            if before == after:
                continue
            original = before.basic.position if before is not None else self.protocol.message('tgf.DItemPosition')
            updated = after.basic.position if after is not None else self.protocol.message('tgf.DItemPosition')
            width, height, _ = self.inventory.dimensions((after if after is not None else before).basic.type_id)
            tx.inventory_updates.append([guid, original.type, original.guid, original.x, original.y,
                                         height, width, updated.type, updated.guid, updated.x, updated.y,
                                         after.basic.count if after is not None else 0])
        for guid in previous.keys() - current.keys():
            dirty.dirty_remove_guid.append(guid)
        items = []
        for guid, item in current.items():
            original = previous.get(guid)
            if item != original:
                items.append(item)
                if original is not None and item.basic.position != original.basic.position:
                    dirty.dirty_postion_action.add().CopyFrom(item)
        if items:
            group = dirty.dirty_items.add()
            group.element.extend(items)
        old_pages = {p.page_id:p for p in old.user_invetory.inventory_pages}
        for page in tx.profile.user_invetory.inventory_pages:
            if page.page_id not in old_pages:
                added=dirty.dirty_inventory_page.add()
                added.CopyFrom(page)
                del added.item_group[:]
        old_roles = {r.basic.role_guid:r for r in old.user_role}
        for role in tx.profile.user_role:
            prior = old_roles.get(role.basic.role_guid)
            if role != prior:
                dirty.dirty_role.add().CopyFrom(role)
        old_coins = {c.coin_type_id:c.count for c in old.user_invetory.coins_data}
        for coin in tx.profile.user_invetory.coins_data:
            if coin.count != old_coins.get(coin.coin_type_id):
                dirty.dirty_coin_data.add().CopyFrom(coin)
        if dirty.ListFields():
            notifications.append(('player_dirty_change', dirty))
        return dirty

    def rpc_inventory_page_add(self, tx, request, result, notifications):
        self.ensure_lobby_inventory(tx)
        self.inventory.add_page(tx)
        self.inventory_notice(tx, notifications)

    def rpc_inventory_item_move(self, tx, request, result, notifications):
        self.ensure_lobby_inventory(tx)
        if not 1 <= len(request.move_actions) <= 512:
            raise RpcError('转移物品请求数量无效')
        for action in request.move_actions:
            original = tx.profile.SerializeToString(deterministic=True)
            tx.db.execute('SAVEPOINT inventory_move')
            try:
                placed, count = self.inventory.move(tx, action.item_basic, action.goal_position)
            except ValueError:
                tx.db.execute('ROLLBACK TO inventory_move')
                tx.profile.ParseFromString(original)
                result.failed_actions.add().CopyFrom(action)
            else:
                entry = result.succ_actions.add()
                entry.CopyFrom(action)
                entry.item_basic.count = count
                entry.goal_position.CopyFrom(placed.basic.position)
            finally:
                tx.db.execute('RELEASE inventory_move')
        self.inventory_notice(tx, notifications)

    def rpc_inventory_item_swap(self, tx, request, result, notifications):
        self.ensure_lobby_inventory(tx)
        self.inventory.swap(tx, request.item_basic, request.swapped_item_basic, request.goal_position,
                            request.specify_swapped_item_new_position if request.HasField('specify_swapped_item_new_position') else None)
        self.inventory_notice(tx, notifications)

    def rpc_role_wear(self, tx, request, result, notifications):
        self.ensure_lobby_inventory(tx)
        self.inventory.wear(tx, request, result)
        self.inventory_notice(tx, notifications)

    def rpc_role_take_off(self, tx, request, result, notifications):
        self.ensure_lobby_inventory(tx)
        self.inventory.take_off(tx, request, result)
        self.inventory_notice(tx, notifications)

    def rpc_role_weapon_set_swap(self, tx, request, result, notifications):
        self.ensure_lobby_inventory(tx)
        self.inventory.swap_weapon_sets(tx, request)
        self.inventory_notice(tx, notifications)

    def rpc_role_create(self, tx, request, result, notifications):
        name = request.role_name.strip()
        if not 1 <= len(name) <= 32:
            raise RpcError('角色名字长度无效')
        if any(role.basic.role_name.casefold() == name.casefold() for role in tx.profile.user_role):
            raise RpcError('已经有同名角色')
        if request.force not in (0, 1):
            raise RpcError('本地角色目前使用冒险者阵营')
        guid = tx.next_guid()
        role = self.game_data.role(request.role_type_id, guid, name, tx.next_item_guid,
                                   request.gender or None, request.force or 1)
        role.deploy_role.backpack.row_size = self.inventory.bag_page_rows * tx.integer('backpack_pages')
        role.basic.create_index = max((r.basic.create_index for r in tx.profile.user_role), default=0) + 1
        role.deploy_role.basic.CopyFrom(role.basic)
        if request.HasField('dress_up'):
            role.deploy_role.dress_up.CopyFrom(request.dress_up)
        tx.profile.user_role.add().CopyFrom(role)
        result.role_guid = guid
        dirty = self.protocol.message('tgf.SCPlayerDirtyChgSync')
        dirty.dirty_role.add().CopyFrom(role)
        notifications.append(('player_dirty_change', dirty))

    def rpc_role_select_skill(self, tx, request, result, notifications):
        role = next((role for role in tx.profile.user_role if role.basic.role_guid == request.role_guid), None)
        if role is None:
            raise RpcError('找不到本地角色')
        role_config = self.game_data.roles[role.basic.type_id]
        if request.new_skill.skill_id not in role_config['role_skill']:
            raise RpcError('这个技能不属于当前职业')
        skill_config = next(row for row in self.game_data.tables['excel_role_skill']
                            if row['skill_id'] == request.new_skill.skill_id)
        position = self.game_data.enum('EnRoleSkillPositionType', skill_config['skill_position'])
        if request.new_skill.key_position != position:
            raise RpcError('技能按键位置不匹配')
        slots = role.deploy_role.role_skill.role_active_skill
        slot = next((slot for slot in slots if slot.key_position == position), None)
        if slot is None:
            slot = slots.add()
        slot.CopyFrom(request.new_skill)
        result.role_guid = role.basic.role_guid
        result.new_skill.CopyFrom(request.new_skill)
        dirty = self.protocol.message('tgf.SCPlayerDirtyChgSync')
        dirty.dirty_role.add().CopyFrom(role)
        notifications.append(('player_dirty_change', dirty))

    def rpc_hall_set_role(self, tx, request, result, notifications):
        if not any(role.basic.role_guid == request.role_guid for role in tx.profile.user_role):
            raise RpcError('local role not found')
        tx.profile.user_hall_setting.role_guid = request.role_guid
        self.game_data.initialize_lobby_links(tx.profile)
        dirty = self.protocol.message('tgf.SCPlayerDirtyChgSync')
        dirty.dirty_hall_setting.CopyFrom(tx.profile.user_hall_setting)
        dirty.dirty_team_snapshot.CopyFrom(tx.profile.user_team)
        notifications.append(('player_dirty_change', dirty))
        expired = self.protocol.message('tgf.SCCacheExpiredSync')
        expired.keys.add(type_id=1, zone_id=1, inst_id=tx.profile.player_info.player.user_id)
        notifications.append(('cache_expired', expired))

    def rpc_player_setting_data(self, tx, request, result, notifications):
        tx.profile.player_info.player_setting.CopyFrom(request.setting_data)

    def rpc_player_update_options(self, tx, request, result, notifications):
        if request.HasField('update_shared_options'):
            tx.profile.player_shared_options.CopyFrom(request.update_shared_options)
        if request.HasField('update_private_options'):
            tx.profile.player_info.private_options.CopyFrom(request.update_private_options)

    def rpc_player_rename(self, tx, request, result, notifications):
        name = request.name.strip()
        if not 1 <= len(name) <= 32:
            raise RpcError('invalid local player name')
        tx.profile.player_info.player.custom_nick_name = name
        tx.profile.player_info.player.nick_name = name

    def rpc_mail_get_all(self, tx, request, result, notifications):
        # This newly-created local profile has no remote or purchased mail.
        pass

    def rpc_friend_get_all(self, tx, request, result, notifications):
        # The offline player has no simulated social accounts.
        pass

    def rpc_pay_get_all(self, tx, request, result, notifications):
        result.pay_data.zone_id = 1
        result.pay_data.balance = 0
        result.pay_data.free_diamond = 0

    def rpc_activity_center_list(self, tx, request, result, notifications):
        result.list.CopyFrom(tx.profile.activity_center_data)

    def rpc_chat_update_panel(self, tx, request, result, notifications):
        result.open = request.open

    def rpc_level_select(self, tx, request, result, notifications):
        if request.level_select.level_type not in {row['level_type'] for row in self.game_data.tables['excel_level_type']}:
            raise RpcError('unknown original map mode')
        tx.profile.user_level.default_level.CopyFrom(request.level_select)
        tx.profile.user_team.current_team.level_select.CopyFrom(request.level_select)
        result.level_select.CopyFrom(request.level_select)

    def merchant_product(self, product_id):
        row = self.game_data.products.get(product_id)
        if row is None or row['market_tab_id'] not in self.game_data.merchant_tabs:
            raise RpcError('本地商人没有这件商品')
        product = row['product']
        if not product.get('is_on_sale') or product.get('sku_id') or not product.get('reward_items'):
            raise RpcError('这件商品暂不提供本地购买')
        # This endpoint sells existing in-game goods for gold. Remote payment
        # products never enter the local merchant's stock or purchase path.
        if not product.get('cost_options') or not any(
            option.get('cost_items') and all(cost['cost_type'] == 1001 for cost in option['cost_items'])
            for option in product['cost_options']):
            raise RpcError('这件商品没有金币价格')
        for reward in product['reward_items']:
            self.inventory.dimensions(reward['type_id'])
            source=self.game_data.sources.get(reward['type_id'],{})
            if row['market_tab_id'] in (1001,1002,1003) and source.get('rarity') not in ('EN_ITEM_RARITY_ROTTED','EN_ITEM_RARITY_NORMAL'):
                raise RpcError('本地商人只售低级装备')
        return row

    def set_merchant_potion_price(self, entry):
        price = self.game_data.merchant_potion_price(entry.product_id)
        if price is None:
            return False
        del entry.cost_options[:]
        entry.cost_options.add().cost_items.add(cost_type=1001, cost_price=price, origin_price=price)
        return True

    def merchant_tab(self, tx, tab_id):
        if tab_id not in self.game_data.merchant_tabs:
            raise RpcError('这个商店页尚未接入本地模式')
        tab = next((t for t in tx.profile.user_market_tabs if t.market_tab_id == tab_id), None)
        if tab is not None and tab.available_products and tab.next_auto_refresh_reset_time > int(time.time()):
            # The native market converter indexes identified_items[0] whenever
            # identified is true. Repair earlier test snapshots before sending
            # them to the client; do not reset stock or discard saved objects.
            for entry in tab.available_products:
                self.set_merchant_potion_price(entry)
                self.merchant_identified_items(tx, entry)
            existing = {entry.product_id for entry in tab.available_products}
            for product_id in sorted(ADVANCED_POTION_PRODUCTS.keys() - existing):
                row = self.game_data.products[product_id]
                if row['market_tab_id'] == tab_id:
                    self.add_merchant_entry(tx, tab, row)
            return tab
        if tab is None:
            tab = tx.profile.user_market_tabs.add(market_tab_id=tab_id)
        del tab.available_products[:]
        del tab.already_buy_products[:]
        tab.last_auto_refresh_reset_time = int(time.time())
        tab.next_auto_refresh_reset_time = int(time.time()) + 300
        rows = sorted((r for r in self.game_data.products.values() if r['market_tab_id'] == tab_id),
                      key=lambda r: (r['product'].get('display_order', 0), r['product_id']))
        for row in rows:
            try:
                self.merchant_product(row['product_id'])
            except (RpcError, ValueError):
                continue
            self.add_merchant_entry(tx, tab, row)
        return tab

    def add_merchant_entry(self, tx, tab, row):
        product = row['product']
        entry = tab.available_products.add(product_id=row['product_id'], available_count=999,
                                           total_available_count=999,
                                           next_auto_refresh_reset_time=tab.next_auto_refresh_reset_time,
                                           last_auto_refresh_reset_time=tab.last_auto_refresh_reset_time,
                                           identified=product.get('identified', False))
        if not self.set_merchant_potion_price(entry):
            for option in product['cost_options']:
                json_format.ParseDict(option, entry.cost_options.add())
        self.merchant_identified_items(tx, entry)

    def merchant_identified_items(self, tx, entry):
        if not entry.identified or entry.identified_items:
            return
        product = self.merchant_product(entry.product_id)['product']
        for reward in product['reward_items']:
            item = entry.identified_items.add()
            item.basic.type_id = reward['type_id']
            item.basic.guid = tx.next_item_guid()
            item.basic.count = reward['count']
            item.data.SetInParent()

    def rpc_market_get_product_list(self, tx, request, result, notifications):
        for tab_id in request.batch_market_tab_ids:
            batch = result.batch_data.add(market_tab_id=tab_id)
            try:
                batch.market_tab_data.CopyFrom(self.merchant_tab(tx, tab_id))
            except RpcError as exc:
                batch.error_code = exc.code

    def rpc_market_reset_refresh_effect(self, tx, request, result, notifications):
        # The original request only acknowledges the merchant's refresh effect.
        pass

    def rpc_market_buy_product(self, tx, request, result, notifications):
        body = request.SerializeToString(deterministic=True)
        fingerprint = hashlib.sha256(body).hexdigest()
        if request.bill_no:
            prior = tx.db.execute('SELECT method,request_sha256,response FROM bills WHERE bill_no=?', (request.bill_no,)).fetchone()
            if prior:
                if prior[0] != 'market_buy_product' or prior[1] != fingerprint:
                    raise RpcError('交易编号已用于其他内容')
                result.ParseFromString(prior[2])
                return
        if not 1 <= request.product_cnt <= 999:
            raise RpcError('购买数量无效')
        row = self.merchant_product(request.product_id)
        product = row['product']
        if product.get('level_trigger', 0) > tx.profile.player_info.player_level:
            raise RpcError('角色等级不足')
        tab = self.merchant_tab(tx, row['market_tab_id'])
        available = next((p for p in tab.available_products if p.product_id == request.product_id), None)
        if available is None or available.available_count < request.product_cnt:
            raise RpcError('商品库存不足')
        if not 0 <= request.optional_costs_index < len(available.cost_options):
            raise RpcError('商品价格选项无效')
        option = available.cost_options[request.optional_costs_index]
        if not option.cost_items or any(cost.cost_type != 1001 or cost.cost_price < 0 for cost in option.cost_items):
            raise RpcError('商品金币价格无效')
        price = sum(cost.cost_price for cost in option.cost_items) * request.product_cnt
        coin = tx.coin()
        if price < 0 or price > coin.count:
            raise RpcError('金币不足')
        for reward in product['reward_items']:
            template = next((i for i in available.identified_items if i.basic.type_id == reward['type_id']), None)
            self.inventory.store_new_item(tx, reward['type_id'], reward['count'] * request.product_cnt,
                                          request.item_storage_type, request.item_storage_guid, template)
        coin.count -= price
        available.available_count -= request.product_cnt
        available.used_count += request.product_cnt
        dirty=self.inventory_notice(tx,notifications)
        if not dirty.ListFields():
            notifications.append(('player_dirty_change',dirty))
        dirty.dirty_market_tabs.add().CopyFrom(tab)
        result.product_id = request.product_id
        if request.bill_no:
            if len(request.bill_no) > 128:
                raise RpcError('交易编号过长')
            tx.db.execute('INSERT INTO bills VALUES(?,?,?,?)', (request.bill_no, 'market_buy_product', fingerprint,
                                                               result.SerializeToString(deterministic=True)))

    def rpc_cache_batch_get(self, tx, request, result, notifications):
        result.client_tag = request.client_tag
        for key in request.keys_with_status:
            if request.cache_type==1 and key.inst_id==NPC_USER and key.zone_id==1:
                player=result.results.add().user_cache
                player.user_key.user_id,player.user_key.zone_id=NPC_USER,1
                player.profile.user_id=NPC_USER
                player.profile.nick_name=player.profile.custom_nick_name='本地虚拟卖家'
            elif request.cache_type == 1 and key.inst_id == tx.profile.player_info.player.user_id and key.zone_id == 1:
                player = result.results.add().user_cache
                player.user_key.user_id = key.inst_id
                player.user_key.zone_id = key.zone_id
                player.account_id = key.inst_id
                player.profile.CopyFrom(tx.profile.player_info.player)
                player.player_level = tx.profile.player_info.player_level
                player.user_data_version = tx.profile.player_info.user_data_version
                player.shared_options.CopyFrom(tx.profile.player_shared_options)
                player.client_version = '1.0.0.97'
                role = next((r for r in tx.profile.user_role if r.basic.role_guid == tx.profile.user_hall_setting.role_guid), None)
                if role:
                    player.player_status_cache.current_role_guid = role.basic.role_guid
                    player.player_status_cache.current_role_type_id = role.basic.type_id
                    player.player_status_cache.current_role_meta.CopyFrom(role.basic.meta)
                    player.player_status_cache.team_id = tx.profile.user_team.current_team.team_key.team_id
                    player.player_status_cache.team_number_count = 1
            elif request.cache_type in (4, 5) and key.zone_id == 1:
                role = next((r for r in tx.profile.user_role if r.basic.role_guid == key.inst_id), None)
                if role is None:
                    result.not_found_keys.add(type_id=request.cache_type, zone_id=key.zone_id, inst_id=key.inst_id)
                    continue
                record = result.results.add()
                if request.cache_type == 5:
                    record.role_detail.user_key.user_id = tx.profile.player_info.player.user_id
                    record.role_detail.user_key.zone_id = 1
                    record.role_detail.role.CopyFrom(role)
                else:
                    basic = record.role_basic
                    basic.user_key.user_id = tx.profile.player_info.player.user_id
                    basic.user_key.zone_id = 1
                    for field in role.basic.DESCRIPTOR.fields:
                        if field.name in basic.role.DESCRIPTOR.fields_by_name:
                            if field.message_type:
                                getattr(basic.role, field.name).CopyFrom(getattr(role.basic, field.name))
                            else:
                                setattr(basic.role, field.name, getattr(role.basic, field.name))
                    basic.role.role_level = role.deploy_role.role_level
                    basic.role.dress_up.CopyFrom(role.deploy_role.dress_up)
                    for item in role.deploy_role.wearing.items:
                        basic.role.wearing.add(type_id=item.basic.type_id, x=item.basic.position.x, y=item.basic.position.y)
            else:
                result.not_found_keys.add(type_id=request.cache_type, zone_id=key.zone_id, inst_id=key.inst_id)
