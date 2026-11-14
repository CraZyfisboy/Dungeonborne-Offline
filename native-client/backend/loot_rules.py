"""Editable per-source loot rules, compiled into the original native tables."""
from __future__ import annotations

import hashlib
import json
import math

from equipment_balance import ARMOR, LEGENDARY, UNIQUE
from inventory import CONSUMABLE_STACK_LIMIT
from protocol import PROJECT, NATIVE


HARD_EXIT_STONE = 2100000
SECOND_FLOOR_LOOT_TYPE = 71


class LootRules:
    def __init__(self, game_data):
        self.data = game_data
        self.presets = json.loads((NATIVE / 'config/loot-presets.json').read_text(encoding='utf8'))
        self.sources = {r['id']: r for r in game_data.tables['excel_loot_source_map']}
        strings = json.loads((PROJECT / 'runtime/_data/cache/config/strings.json').read_text(encoding='utf8'))
        self.strings = strings
        enum = game_data.protocol.pool.FindEnumTypeByName('tgf.EnItemRarity')
        self.rarities = {r.number: strings['GeneralStringTable.csv'].get('Rarity_' + str(r.number), r.name)
                         for r in enum.values if r.number > 0}
        high_rarities = {game_data.enum('EnItemRarity', rarity) for rarity in (LEGENDARY, UNIQUE)}
        self.rare_armor = {rarity: [] for rarity in high_rarities}
        self.retired_necklaces = game_data.equipment_balance.retired_necklaces
        self.catalog = {}
        reserved_gear = {r['inst_id'] for r in game_data.tables['excel_gear_heirloom_guide']}
        reserved_gear.update(r['id'] for r in game_data.tables['excel_mithril_force_item'])
        for item_id, row in game_data.items.items():
            # These require crafting/ownership or faction-specific deployment.
            # Native loot tables cannot supply that ownership metadata.
            if ((item_id in reserved_gear and row['item_major_type'] == 'EN_ITEM_MAJOR_TYPE_EQUIPMENT')
                    or 'HEIRLOOM' in row.get('skin_third_sort', '') or item_id in self.retired_necklaces):
                continue
            source = game_data.sources.get(row.get('ue_source_type_id') or item_id)
            if source is None or row['item_major_type'] not in (
                    'EN_ITEM_MAJOR_TYPE_EQUIPMENT', 'EN_ITEM_MAJOR_TYPE_CONSUMABLES',
                    'EN_ITEM_MAJOR_TYPE_CRAFTS', 'EN_ITEM_MAJOR_TYPE_GEM'):
                continue
            if row.get('is_forbidden_bring_into_battle'):
                continue
            if row['item_major_type'] == 'EN_ITEM_MAJOR_TYPE_EQUIPMENT' and (
                    not source.get('ue_path') or min(source['position_cfg'].values()) < 1):
                continue
            rarity = game_data.enum('EnItemRarity', source['rarity'])
            name = game_data.equipment_balance.necklace_display_names.get(item_id)
            self.catalog[item_id] = dict(id=item_id, name=name or strings['ItemStringTable.csv'].get(row.get('name'), str(item_id)),
                                        rarity=rarity, kind='equipment' if row['item_major_type'] == 'EN_ITEM_MAJOR_TYPE_EQUIPMENT' else 'items',
                                        stack=CONSUMABLE_STACK_LIMIT if source['type_id'] in game_data.extended_stack_ids
                                        else source['position_cfg']['accumulation_limit'])
            if source['item_type'] == ARMOR and rarity in self.rare_armor:
                self.rare_armor[rarity].append(item_id)

    @staticmethod
    def category(name):
        lower = name.lower()
        if any(x in lower for x in ('monster', 'werewolf', 'beholder', 'm_insect')):
            return 'monster'
        if any(x in lower for x in ('chest', 'box', 'coffin')) and 'noloot' not in lower:
            return 'chest'
        return 'other'

    def rank(self, source_id):
        name = self.sources[source_id]['name']
        if source_id in self.presets['gold_chest_source_ids']:
            return 'gold_chest'
        if any(s in name for s in self.presets['summoned_name_fragments']):
            return 'summoned'
        if source_id in self.presets['shadow_boss_source_ids']:
            return 'shadow_boss'
        if source_id in self.presets['boss_source_ids']:
            return 'boss'
        if source_id in self.presets['elite_source_ids']:
            return 'elite'
        return 'chest' if self.category(name) == 'chest' else 'normal'

    def default(self, source_id, difficulty=1):
        if source_id not in self.sources:
            raise ValueError('找不到原版掉落来源')
        self.integer(difficulty, 1, 3, '地图难度')
        category = self.category(self.sources[source_id]['name'])
        rank = self.rank(source_id)
        preset = self.presets['difficulties'][str(difficulty)]
        quality = preset.get(f'{rank}_weights', preset['rarity_weights'])
        target_armor = rank == 'gold_chest'
        enabled = category != 'other' and rank != 'summoned'
        bonus = preset.get(f'{rank}_bonus', {})
        items = [
            dict(item_id=100013, chance=preset['potion_chance'], rolls=1, count=1),
            dict(item_id=100016, chance=preset['potion_chance'], rolls=1, count=1),
            dict(item_id=100018, chance=preset['potion_chance'], rolls=1, count=1),
            dict(item_id=100060, chance=preset['material_chance'], rolls=1, count=1),
        ] if enabled else []
        if enabled and rank == 'gold_chest' and difficulty == 3:
            items.insert(0, dict(item_id=HARD_EXIT_STONE, chance=100, rolls=1, count=1))
        return dict(version=1, enabled=enabled, equipment_pool=dict(
            chance=preset['equipment_chance'].get(rank, 0) if enabled else 0,
            rolls=self.presets['equipment_rolls'].get(rank, 1),
            rarities=[dict(rarity=r, weight=quality[str(r)],
                           item_ids=sorted(self.rare_armor.get(r, ())) if target_armor and quality[str(r)] else [])
                      for r in self.rarities]),
            rarities=[dict(rarity=int(r), chance=chance, rolls=1, count=1, kind='equipment', item_ids=[])
                      for r, chance in bonus.items()], items=items)

    def second_floor_equipment(self, source_id, difficulty):
        if self.category(self.sources[source_id]['name']) == 'other':
            return None
        rank = self.rank(source_id)
        preset = self.presets['second_floor']
        chance = preset['equipment_chance'].get(rank, 0)
        if not chance:
            return None
        weights = preset['rarity_weights'][str(difficulty)]
        return dict(chance=chance, rolls=1, rarities=[
            dict(rarity=rarity, weight=weights[str(rarity)],
                 item_ids=sorted(self.rare_armor.get(rarity, ())) if rank == 'gold_chest' else [])
            for rarity in sorted(self.rarities) if weights.get(str(rarity), 0)])

    @staticmethod
    def integer(value, low, high, label):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(label + '无效')
        return value

    @staticmethod
    def chance(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('掉落率必须在 0～100% 之间')
        return round(value, 2)

    def pool(self, rule):
        ids = rule['item_ids'] or [i for i, row in self.catalog.items()
                                  if row['rarity'] == rule['rarity'] and (rule['kind'] == 'both' or row['kind'] == rule['kind'])]
        return sorted(ids)

    def validate(self, source_id, value):
        if source_id not in self.sources or not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError('掉落配置版本或来源无效')
        if type(value.get('enabled')) is not bool:
            raise ValueError('掉落开关无效')
        rarities, items = value.get('rarities'), value.get('items')
        if not isinstance(rarities, list) or not isinstance(items, list) or len(rarities) > len(self.rarities) or len(items) > 128:
            raise ValueError('掉落表条目数量无效')
        normalized = dict(version=1, enabled=value['enabled'], equipment_pool=None, rarities=[], items=[])
        equipment = value.get('equipment_pool')
        if equipment is not None:
            if not isinstance(equipment, dict) or not isinstance(equipment.get('rarities'), list):
                raise ValueError('装备品质池无效')
            entry = dict(chance=self.chance(equipment.get('chance')),
                         rolls=self.integer(equipment.get('rolls'), 1, 10, '装备判定次数'), rarities=[])
            seen_quality = set()
            for row in equipment['rarities']:
                if not isinstance(row, dict) or type(row.get('rarity')) is not int or row['rarity'] not in self.rarities or row['rarity'] in seen_quality:
                    raise ValueError('装备品质无效或重复')
                rarity = row['rarity']
                seen_quality.add(rarity)
                ids = row.get('item_ids')
                if not isinstance(ids, list) or len(ids) > len(self.catalog) or any(
                        type(i) is not int or i not in self.catalog or self.catalog[i]['rarity'] != rarity
                        or self.catalog[i]['kind'] != 'equipment' for i in ids):
                    raise ValueError('装备池中有不匹配的物品')
                quality = dict(rarity=rarity, weight=self.chance(row.get('weight')), item_ids=sorted(set(ids)))
                if quality['weight'] and not self.pool(dict(rarity=rarity, kind='equipment', item_ids=quality['item_ids'])):
                    raise ValueError('该品质没有可生成的原版装备')
                entry['rarities'].append(quality)
            if sum(round(r['weight'] * 100) for r in entry['rarities']) != 10000:
                raise ValueError('装备品质概率合计必须为 100%')
            normalized['equipment_pool'] = entry
        seen = set()
        for row in rarities:
            if not isinstance(row, dict):
                raise ValueError('品质掉落条目无效')
            rarity = row.get('rarity')
            if rarity not in self.rarities or rarity in seen:
                raise ValueError('物品品质无效或重复')
            seen.add(rarity)
            kind = row.get('kind')
            if kind not in ('items', 'equipment', 'both'):
                raise ValueError('请选择物品、装备或两者')
            ids = row.get('item_ids')
            if not isinstance(ids, list) or len(ids) > len(self.catalog):
                raise ValueError('指定物品清单无效')
            if any(type(i) is not int or i not in self.catalog or self.catalog[i]['rarity'] != rarity
                   or (kind != 'both' and self.catalog[i]['kind'] != kind) for i in ids):
                raise ValueError('指定物品不符合这行的品质或类别')
            entry = dict(rarity=rarity, chance=self.chance(row.get('chance')),
                         rolls=self.integer(row.get('rolls'), 1, 10, '判定次数'),
                         count=self.integer(row.get('count'), 1, 999, '掉落数量'), kind=kind, item_ids=sorted(set(ids)))
            choices = self.pool(entry)
            if entry['chance'] and not choices:
                raise ValueError('该品质和类别没有可生成的原版物品')
            if any(entry['count'] > self.catalog[i]['stack'] for i in choices):
                raise ValueError('数量超过原版单格堆叠上限；多件装备请增加判定次数')
            normalized['rarities'].append(entry)
        for row in items:
            if not isinstance(row, dict) or type(row.get('item_id')) is not int or row['item_id'] not in self.catalog:
                raise ValueError('指定掉落物品不存在')
            item_id = row['item_id']
            normalized['items'].append(dict(item_id=item_id, chance=self.chance(row.get('chance')),
                                           rolls=self.integer(row.get('rolls'), 1, 10, '判定次数'),
                                           count=self.integer(row.get('count'), 1, self.catalog[item_id]['stack'], '掉落数量')))
        normalized['rarities'].sort(key=lambda row: row['rarity'])
        return normalized

    def get(self, tx, source_id, difficulty=1):
        self.integer(difficulty, 1, 3, '地图难度')
        row = tx.db.execute('SELECT revision,data FROM difficulty_drop_rules WHERE source_id=? AND difficulty=?', (source_id, difficulty)).fetchone()
        if row:
            value = json.loads(row[1])
            equipment = value.get('equipment_pool') or {}
            for rarity in value['rarities'] + equipment.get('rarities', []):
                rarity['item_ids'] = [self.retired_necklaces.get(i, i) for i in rarity['item_ids']]
            for item in value['items']:
                item['item_id'] = self.retired_necklaces.get(item['item_id'], item['item_id'])
            return row[0], self.validate(source_id, value)
        return 0, self.default(source_id, difficulty)

    def save(self, tx, source_id, rules, expected_revision, difficulty=1):
        rules = self.validate(source_id, rules)
        revision, _ = self.get(tx, source_id, difficulty)
        if expected_revision != revision:
            raise ValueError('配置已被另一窗口修改，请重新读取')
        revision += 1
        tx.db.execute('INSERT INTO difficulty_drop_rules VALUES(?,?,?,?) ON CONFLICT(source_id,difficulty) DO UPDATE SET revision=excluded.revision,data=excluded.data',
                      (source_id, difficulty, revision, json.dumps(rules, ensure_ascii=False, sort_keys=True)))
        return revision

    def snapshot(self, tx):
        multiplier = tx.integer('drop_multiplier')
        self.integer(multiplier, 1, 10, '全局掉率倍率')
        difficulties, overrides = {}, {}
        for difficulty in (1, 2, 3):
            key = str(difficulty)
            difficulties[key], overrides[key] = {}, []
            for source_id in sorted(self.sources):
                revision, config = self.get(tx, source_id, difficulty)
                difficulties[key][str(source_id)] = config
                if revision:
                    overrides[key].append(source_id)
        return dict(version=2, multiplier=multiplier, difficulties=difficulties, overrides=overrides)


def compile_native_loot(snapshot, rules):
    """Generate new table bytes; never alter original resources or any live pak."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    raw = PROJECT / 'analysis/raw-config/Dungeonborne/Content/AdditionalData'
    pool = descriptor_pool.DescriptorPool()
    for descriptor in descriptor_pb2.FileDescriptorSet.FromString((raw / 'Protocol/Configure.pb').read_bytes()).file:
        pool.Add(descriptor)
    envelope = message_factory.GetMessageClass(pool.FindMessageTypeByName('org.xresloader.pb.xresloader_datablocks'))
    wrappers = {}
    types = {}
    for name in ['loot', 'loot_rate', 'loot_choose_group_rate', 'loot_ue_source_choose_group']:
        wrappers[name] = envelope.FromString((raw / f'ExcelConfig/{name}.bytes').read_bytes())
        types[name] = message_factory.GetMessageClass(pool.FindMessageTypeByName(wrappers[name].data_message_type))
    if snapshot.get('version') != 2:
        raise ValueError('unsupported native loot snapshot version')
    replaced = {int(i) for table in snapshot['difficulties'].values() for i in table}
    source_rows = [block for block in wrappers['loot_ue_source_choose_group'].data_block
                   if types['loot_ue_source_choose_group'].FromString(block).ue_loot_source_id not in replaced]
    del wrappers['loot_ue_source_choose_group'].data_block[:]
    wrappers['loot_ue_source_choose_group'].data_block.extend(source_rows)
    serial = 920000000
    counts = dict(sources=0, second_floor_sources=0, rolls=0, pool_rows=0)
    pool_cache = {}
    floor_sources = {}
    def item_pool(items, count):
        nonlocal serial
        key = (tuple(items), count)
        if key in pool_cache:
            return pool_cache[key]
        if not items or len(items) > 30000:
            raise ValueError('invalid native item pool size')
        serial += 1
        random_id = serial
        for index, item_id in enumerate(items):
            weight = 30000 // len(items) + (index < 30000 % len(items))
            drop = types['loot'](random_id=random_id, item_id=item_id, cnt=count, level=1, rate=weight)
            wrappers['loot'].data_block.append(drop.SerializeToString(deterministic=True))
            counts['pool_rows'] += 1
        wrappers['loot_rate'].data_block.append(types['loot_rate'](random_id=random_id).SerializeToString(deterministic=True))
        pool_cache[key] = random_id
        return random_id

    def add_rolls(group_ids, pools, weights, chance, rolls):
        nonlocal serial
        effective = round(chance * 100) * snapshot['multiplier']
        guaranteed, remainder = divmod(effective, 10000)
        probabilities = [10000] * guaranteed + ([remainder] if remainder else [])
        for _ in range(rolls):
            for probability in probabilities:
                serial += 1
                group_ids.append(serial)
                group = types['loot_choose_group_rate'](loot_id=serial, random_ids=pools, pr=weights,
                                                       default_rate=probability, max_rate=probability)
                wrappers['loot_choose_group_rate'].data_block.append(group.SerializeToString(deterministic=True))
                counts['rolls'] += 1

    def add_equipment(group_ids, equipment):
        if not equipment or not equipment['chance']:
            return
        pools, weights = [], []
        for quality in equipment['rarities']:
            if not quality['weight']:
                continue
            ids = rules.pool(dict(rarity=quality['rarity'], kind='equipment', item_ids=quality['item_ids']))
            pools.append(item_pool(ids, 1))
            weights.append(round(quality['weight'] * 100))
        add_rolls(group_ids, pools, weights, equipment['chance'], equipment['rolls'])

    def add_source(source_id, map_ids, loot_type, group_ids):
        for map_id in map_ids:
            source = types['loot_ue_source_choose_group'](ue_loot_source_id=source_id, map_id=map_id,
                                                         loot_type=loot_type, loot_ids=group_ids)
            wrappers['loot_ue_source_choose_group'].data_block.append(source.SerializeToString(deterministic=True))
        counts['second_floor_sources' if loot_type else 'sources'] += len(map_ids)

    for difficulty, table in sorted(snapshot['difficulties'].items()):
        map_ids = list(rules.presets['difficulty_map'][difficulty])
        overridden = set(snapshot.get('overrides', {}).get(difficulty, ()))
        if difficulty == '1':
            map_ids.append(0)  # Original standalone fallback; real difficulty binding is verified separately.
        for source_id, config in sorted(table.items(), key=lambda item: int(item[0])):
            source_id = int(source_id)
            config = rules.validate(source_id, config)
            group_ids = []
            if config['enabled']:
                # Give exit stones space before large equipment can fill the chest.
                for row in config['items']:
                    if row['item_id'] == HARD_EXIT_STONE and row['chance']:
                        pool_id = item_pool([HARD_EXIT_STONE], row['count'])
                        add_rolls(group_ids, [pool_id], [100], row['chance'], row['rolls'])
                add_equipment(group_ids, config['equipment_pool'])
                rows = [(row, rules.pool(row)) for row in config['rarities']]
                rows += [(row, [row['item_id']]) for row in config['items'] if row['item_id'] != HARD_EXIT_STONE]
                for row, items in rows:
                    if row['chance']:
                        random_id = item_pool(items, row['count'])
                        add_rolls(group_ids, [random_id], [100], row['chance'], row['rolls'])
            add_source(source_id, map_ids, 0, group_ids)
            if config['enabled'] and source_id not in overridden:
                bonus_ids = []
                add_equipment(bonus_ids, rules.second_floor_equipment(source_id, int(difficulty)))
                if bonus_ids:
                    add_source(source_id, map_ids, SECOND_FLOOR_LOOT_TYPE, bonus_ids)
                    for map_id in map_ids:
                        floor_sources.setdefault(map_id, set()).add(rules.sources[source_id]['name'])
    payloads = {}
    for name, wrapper in wrappers.items():
        wrapper.header.count = len(wrapper.data_block)
        wrapper.header.hash_code = 'sha256:' + hashlib.sha256(b''.join(wrapper.data_block)).hexdigest()
        payloads[f'Dungeonborne/Content/AdditionalData/ExcelConfig/{name}.bytes'] = wrapper.SerializeToString(deterministic=True)
    lines = [f'return {{loot_type={SECOND_FLOOR_LOOT_TYPE}, maps={{']
    for map_id, names in sorted(floor_sources.items()):
        lines.append(f'    [{map_id}]={{')
        lines.extend(f'        [{json.dumps(name)}]=true,' for name in sorted(names))
        lines.append('    },')
    lines.append('}}')
    payloads['Dungeonborne/Content/Script/OfflineProbe/FloorLootRules.lua'] = ('\n'.join(lines) + '\n').encode('ascii')
    return payloads, counts
