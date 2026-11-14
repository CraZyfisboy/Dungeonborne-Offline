"""Original configuration-backed construction of native protobuf objects."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
import time

from protocol import PROJECT
from inventory import Inventory
from equipment_balance import EquipmentBalance

LOCAL_MERCHANT_TABS=(1001,1002,1003,1004)
BANDAGE_IDS = frozenset({100016, 100073})
ADVANCED_POTION_PRODUCTS = {704010: 100106, 704011: 100107, 704012: 100108}
POTION_UNIT_PRICE = 50


class GameData:
    def __init__(self, protocol):
        self.protocol = protocol
        self.tables = json.loads((PROJECT / 'runtime/_data/cache/config/tables.json').read_text(encoding='utf-8'))
        self.roles = {row['type_id']: row for row in self.tables['excel_role_type']
                      if row.get('environment') == 'EN_ENVIRONMENT_TYPE_ALL_AVAILABLE'}
        attribute_ids = {row['name'].removeprefix('affix_'): row['attribute_type']
                         for row in self.tables['excel_battle_attribute_description']}
        self.role_attributes = defaultdict(dict)
        for row in self.tables['excel_role_level_upgrade']:
            self.role_attributes[row['role_type_id']][row['role_level']] = {
                attribute_ids[name]: value['fixed_value'] for name, value in row['role_attribute'].items()
                if name != 'ability_set'
            }
        self.role_exp_limits = {row['level']: row['max_exp'] for row in self.tables['excel_role_level_exp']}
        self.map_experience = defaultdict(list)
        for row in self.tables['excel_map_exp']:
            self.map_experience[row['map_id']].append((int(row['alive_time']), row['exp']))
        self.equipment = {row['config_id']: row for row in self.tables['excel_role_default_equipment']}
        self.items = {row['type_id']: row for row in self.tables['excel_item_type']}
        self.sources = {row['type_id']: row for row in self.tables['uesource_item_type']}
        self.potion_ids = {row['type_id'] for row in self.sources.values()
                           if row['slot_type'] == 'EN_SKIN_SLOT_TYPE_POTION'
                           and row['item_type'] == 'EN_ITEM_UE_TYPE_ITEM_CONSUMABLE'}
        self.extended_stack_ids = self.potion_ids | BANDAGE_IDS
        self.constants = {row['key']: row['value'] for row in self.tables['excel_origin_const_config']}
        self.products = {row['product_id']: row for row in self.tables['excel_market_product_item']}
        self.merchant_tabs = set(LOCAL_MERCHANT_TABS)
        for product_id, item_id in ADVANCED_POTION_PRODUCTS.items():
            row = self.products[product_id]
            product = row['product']
            reward, = product['reward_items']
            if item_id not in self.potion_ids or row['market_tab_id'] not in self.merchant_tabs:
                raise ValueError(f'invalid original advanced potion listing: {product_id}')
            self.products[product_id] = dict(row, refresh_pool_id=0, product=dict(
                product, is_on_sale=True, reward_items=[dict(reward, type_id=item_id)],
                refresh_count=999, refresh_pool_id=0, random_weight=0))
        self.recipes = {row['id']: row for row in self.tables['excel_crafting_recipe']}
        self.equipment_balance = EquipmentBalance(self)
        self.tables['ue_source_equipment'] = [
            self.equipment_balance.necklace_equipment.get(row['equip_id'], row)
            for row in self.tables['ue_source_equipment']
        ]

    def enum(self, name, value):
        return self.protocol.pool.FindEnumTypeByName('tgf.' + name).values_by_name[value].number

    def merchant_potion_price(self, product_id):
        row = self.products.get(product_id, {})
        rewards = row.get('product', {}).get('reward_items', ())
        if rewards and all(reward['type_id'] in self.potion_ids for reward in rewards):
            return POTION_UNIT_PRICE * sum(reward['count'] for reward in rewards)
        return None

    def set_role_attributes(self, deploy):
        levels = self.role_attributes.get(deploy.basic.type_id)
        if not levels:
            raise ValueError('找不到该职业的原版等级属性')
        level = max((level for level in levels if level <= deploy.role_level), default=min(levels))
        deploy.role_level = level
        deploy.history_role_max_level = max(deploy.history_role_max_level, level)
        deploy.history_role_max_total_level = max(deploy.history_role_max_total_level, level)
        deploy.role_affix.attribute.update(levels[level])

    def grant_role_experience(self, deploy, map_id, alive_seconds):
        _, experience = max((row for row in self.map_experience.get(map_id, ()) if row[0] <= alive_seconds),
                            default=(0, 0))
        maximum = max(self.role_attributes[deploy.basic.type_id])
        deploy.exp += experience
        while deploy.role_level < maximum and deploy.exp >= self.role_exp_limits[deploy.role_level]:
            deploy.exp -= self.role_exp_limits[deploy.role_level]
            deploy.role_level += 1
        if deploy.role_level == maximum:
            deploy.exp = min(deploy.exp, self.role_exp_limits[maximum])
        self.set_role_attributes(deploy)
        return experience

    def dungeon(self, selection):
        # The lobby's map_id is a UI family, not the DS map-detail key (2 is
        # the test gym there). Use the shipped PvE scene variants explicitly.
        selected = next((r for r in self.tables['excel_map_select']
                         if r['level_type'] == selection.level_type and r['level_id'] == selection.level_id), None)
        if selected is None or selected['map_id'] not in (1, 2) or selected['difficulty'] not in (1, 2, 7):
            raise ValueError('当前本地入口支持地下墓穴与城堡的单人地牢')
        variant = {1: 1, 2: 2, 7: 3}[selected['difficulty']]
        detail_id = 10000 + selected['map_id'] * 100 + variant
        detail = next(r for r in self.tables['excel_map_detail'] if r['map_id'] == detail_id)
        scene = next(r['ue_path'] for r in detail['map_config']
                     if r['room_type'] == 'EN_DS_BATTLE_SCENES_TYPE_BATTLE_ROOM_LEVEL_ONE')
        competition = next(r for r in self.tables['excel_competition_level']
                           if r['competition_level_id'] == selection.level_id and r['season_id'] == 1)
        return detail, scene, competition

    def role(self, type_id, guid, name, next_guid, gender=None, force=1, failed=False):
        config = self.roles.get(type_id)
        if config is None:
            raise ValueError('role is not an original released character: ' + str(type_id))
        role = self.protocol.message('tgf.DUserRole')
        role.basic.type_id = type_id
        role.basic.role_guid = guid
        role.basic.role_name = name
        role.basic.create_index = guid
        role.basic.create_timepoint = int(time.time())
        for key, enum in [('race', 'EnRoleRaceType'), ('career', 'EnRoleCareerType')]:
            setattr(role.basic.meta, key, self.enum(enum, config['meta'][key]))
        allowed = [self.enum('EnRoleGenderType', v) for v in config['available_gender']]
        role.basic.meta.gender = allowed[0] if gender is None else gender
        if role.basic.meta.gender not in allowed:
            raise ValueError('gender is not available for the original role')
        role.basic.meta.force = force
        role.deploy_role.basic.CopyFrom(role.basic)
        role.deploy_role.role_level = 1
        self.set_role_attributes(role.deploy_role)
        role.deploy_role.backpack.row_size = int(self.constants['role_backpack_default_row_size'])
        role.deploy_role.backpack.column_size = int(self.constants['role_backpack_defualt_column_size'])
        pouch_roles = {int(v) for v in self.constants['enable_small_backpack_role_type_id_list'].split(',') if v.strip()}
        if type_id in pouch_roles:
            role.deploy_role.small_backpack.row_size = int(self.constants['role_small_backpack_default_row_size'])
            role.deploy_role.small_backpack.column_size = int(self.constants['role_small_backpack_defualt_column_size'])
        for skill in config['role_initial_skill']:
            entry = role.deploy_role.role_skill.role_active_skill.add()
            entry.key_position = self.enum('EnRoleSkillPositionType', skill['key_position'])
            entry.skill_id = skill['skill_id']
        role.deploy_role.role_skill.role_passive_skill.extend(config['role_initial_passive_skill'])
        inventory = Inventory(self.protocol, self)
        storage = self.protocol.pool.FindEnumTypeByName('tgf.EnItemStorageType').values_by_name
        field = 'game_failed_default_item_config_ids' if failed else 'create_default_item_config_ids'
        for config_id in config[field]:
            equipment = self.equipment[config_id]
            name = equipment['storage_type']
            if name == 'EN_ITEM_STORAGE_TYPE_ROLE_WEARING':
                item = role.deploy_role.wearing.items.add()
                item.basic.type_id = equipment['item_id']
                item.basic.guid = next_guid()
                item.basic.count = equipment['count']
                item.basic.position.type = storage[name].number
                item.basic.position.guid = guid
                item.basic.position.x = self.enum('EnItemWearingPart', equipment['wear_part'])
                continue
            if name not in ('EN_ITEM_STORAGE_TYPE_BACKPACK', 'EN_ITEM_STORAGE_TYPE_BACKPACK_WITHOUT_XY', 'EN_ITEM_STORAGE_TYPE_SMALL_BACKPACK'):
                continue
            if name == 'EN_ITEM_STORAGE_TYPE_BACKPACK_WITHOUT_XY':
                name = 'EN_ITEM_STORAGE_TYPE_BACKPACK'
            bag = role.deploy_role.backpack if name == 'EN_ITEM_STORAGE_TYPE_BACKPACK' else role.deploy_role.small_backpack
            width, height, stack = inventory.dimensions(equipment['item_id'])
            remaining = equipment['count']
            while remaining:
                position = inventory.free_position(bag, bag.items, width, height)
                if position is None:
                    raise ValueError('original starter items exceed the role backpack')
                item = bag.items.add()
                item.basic.type_id = equipment['item_id']
                item.basic.guid = next_guid()
                item.basic.count = min(remaining, stack)
                item.basic.position.type = storage[name].number
                item.basic.position.guid = guid
                item.basic.position.x, item.basic.position.y = position
                remaining -= item.basic.count
        return role

    def profile(self):
        profile = self.protocol.message('tgf.SCPlayerGetInfoRsp')
        # Empty native submessages are present so the original cache receives
        # an initial snapshot instead of retaining stale online state.
        for field in profile.DESCRIPTOR.fields:
            if field.message_type and not field.is_repeated:
                getattr(profile, field.name).SetInParent()
        profile.player_info.player.open_id = '10000001'
        profile.player_info.player.user_id = 10000001
        profile.player_info.player.nick_name = '离线冒险者'
        profile.player_info.player.custom_nick_name = '离线冒险者'
        profile.player_info.player_level = 1
        profile.player_info.user_data_version = 1
        page = profile.user_invetory.inventory_pages.add()
        page.row_size = int(self.constants['inventory_default_row_size'])
        page.column_size = int(self.constants['inventory_defualt_column_size'])
        # 1001 is 金币 in the original ItemStringTable.csv.
        profile.user_invetory.coins_data.add(coin_type_id=1001, count=1000)
        counter = iter(range(10000101, 10010000))
        role = self.role(400002, 10000001001, '离线战士', lambda: next(counter))
        profile.user_role.add().CopyFrom(role)
        profile.user_hall_setting.role_guid = role.basic.role_guid
        for row in self.tables['excel_module_unlock_type']:
            # These are the original UI modules. This local profile has no
            # publisher ban or timed server-wide restriction.
            profile.user_unlock.unlock_modules.add(module_id=row['module_id'], unlock_timepoint=int(time.time()))
        profile.user_unlock.is_circular_queue_empty = True
        self.initialize_lobby_links(profile)
        return profile

    def initialize_lobby_links(self, profile):
        """Fill missing local-only initial links without replacing saved items."""
        role = next((r for r in profile.user_role if r.basic.role_guid == profile.user_hall_setting.role_guid), None)
        if role:
            team = profile.user_team.current_team
            team.team_key.team_id = profile.player_info.player.user_id
            team.captain_user_key.user_id = profile.player_info.player.user_id
            team.captain_user_key.zone_id = 1
            member = next((m for m in team.members if m.user_key.user_id == profile.player_info.player.user_id), None)
            if member is None:
                member = team.members.add()
            member.user_key.CopyFrom(team.captain_user_key)
            member.deploy_role.role_type_id = role.basic.type_id
            member.deploy_role.meta.CopyFrom(role.basic.meta)
            member.deploy_role.role_level = role.deploy_role.role_level
            member.client_version = '1.0.0.97'
            member.joined_team_timepoint = role.basic.create_timepoint
            team.level_select.CopyFrom(profile.user_level.default_level)
        if not profile.user_crafting_data.recipe:
            profile.user_crafting_data.recipe.extend(row['id'] for row in self.recipes.values()
                                                      if not row.get('unlock_condition'))
