"""Balance for high-rarity equipment and playable legendary necklaces."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


BASIC = 'EN_BATTLE_AFFIX_TYPE_BASIC'
MINOR = 'EN_BATTLE_AFFIX_TYPE_MINOR'
EQUIPPED = 'EN_BATTLE_AFFIX_EFFECTIVE_LOCATION_ROLE_EQUIP'
LEGENDARY_VALUE_OFFSET = 30000
UNIQUE_VALUE_OFFSET = 31000
NECKLACE_VALUE_OFFSET = 32000
NECKLACE_VARIANT_OFFSET = 100
LEGENDARY = 'EN_ITEM_RARITY_LEGENDARY'
UNIQUE = 'EN_ITEM_RARITY_UNIQUE'
EXCEPTIONAL = 'EN_ITEM_RARITY_EXCEPTIONAL'
ARMOR = 'EN_ITEM_UE_TYPE_EQUIP_ARMOR'
WEAPON = 'EN_ITEM_UE_TYPE_EQUIP_WEAPON'
ACCESSORY = 'EN_ITEM_UE_TYPE_EQUIP_ACCESSORY'
SET_POOL_MEMBERS = {
    101201: (11001, 11004, 11007, 11008),
    100702: (11002, 11003, 11005, 11006),
}
UNIQUE_ACCESSORY_STATS = {185015001: 'will', 185026000: 'intelligence', 185026001: 'strength'}
ORDINARY_FIXED_FIELDS = frozenset({
    'attack_right_weapon_attack', 'attack_right_att_gear_level', 'defend_def_gear_level',
    'first_level_strength', 'first_level_dexterity', 'first_level_stamina', 'first_level_intelligence',
    'first_level_will', 'first_level_faith',
})
SCORE_FIELDS = {'attack_right_att_gear_level': 'attack_score', 'defend_def_gear_level': 'defend_score'}
LEGENDARY_NECKLACE_STATS = {
    185025000: 'dexterity',
    185025001: 'intelligence',
    185025002: 'intelligence',
    185025003: 'will',
    185025004: 'faith',
    185025005: 'strength',
    185025006: 'faith',
}
NECKLACE_NAMES = (
    '逐影之眼', '风暴坠饰', '焚星护符', '霜心吊坠', '圣辉之印', '斗魂勋章', '纯净誓约',
)
CLASS_NECKLACE_REPLACEMENTS = {
    1: 185025005, 3: 185025000, 5: 185025004, 6: 185025003,
    7: 185025005, 8: 185025000, 9: 185025002, 10: 185025006,
    11: 185025005, 12: 185025000, 13: 185025004,
}


def minor_rule(*pool_ids):
    return dict(affix_type=MINOR, affix_count_pool_id=101, location=EQUIPPED,
                pool_random=[dict(affix_pool_id=list(pool_ids))])


def boosted(value, unit):
    steps = Decimal(str(value)) * Decimal('1.20') / unit
    return float(steps.to_integral_value(rounding=ROUND_CEILING) * unit)


def interval_above(bounds, maximum, unit):
    if maximum is None or bounds['min_value'] > maximum or bounds['min_value'] <= 0 or unit <= 0:
        return bounds
    unit = Decimal(str(unit))
    minimum = ((Decimal(str(maximum)) / unit).to_integral_value(rounding=ROUND_FLOOR) + 1) * unit
    span = Decimal(str(bounds['max_value'])) - Decimal(str(bounds['min_value']))
    return {'min_value': float(minimum), 'max_value': float(minimum + span)}


def upgrade_value_row(original, value_id, rarity, set_points, is_gear_set, compare_at=None):
    row = deepcopy(original)
    row['affix_value_id'] = value_id
    unit = Decimal(str(row['data']['unit']))
    previous, upgraded = str(rarity - 1 if compare_at is None else compare_at), str(rarity)
    for affix in row['data']['affix_data']:
        ranges = affix['rarity_range']
        current = ranges.get(upgraded)
        if not current:
            continue
        if is_gear_set:
            if affix['affix_type'] == MINOR:
                ranges[upgraded] = dict(min_value=set_points[0], max_value=set_points[1])
        elif unit > 0 and current['min_value'] > 0:
            comparable = ranges.get(previous, current)
            ranges[upgraded] = {key: boosted(max(value, comparable[key]), unit)
                                for key, value in current.items()}
    return row


class EquipmentBalance:
    def __init__(self, data):
        originals = {row['item_id']: row for row in data.tables['excel_affix_template']}
        self.added_items = {}
        self.added_sources = {}
        self.add_dark_gold_armor(data, originals)
        self.necklace_variants = {}
        self.necklace_display_names = {}
        for donor, name in zip(LEGENDARY_NECKLACE_STATS, NECKLACE_NAMES, strict=True):
            variant = donor + NECKLACE_VARIANT_OFFSET
            self.clone_variant(data, originals, donor, variant, LEGENDARY)
            self.added_items[variant]['name'] = f'Item_Name_{variant}'
            self.necklace_variants[donor] = variant
            self.necklace_display_names[variant] = name
        data.items.update(self.added_items)
        data.sources.update(self.added_sources)
        self.templates = {}
        self.bonus_rule_start = {}
        self.bonus_rule_count = {}
        self.counts = Counter()
        self.fixed_adjustments = {}
        self.high_rarity = {}
        self.necklace_stats = {}
        self.retired_necklaces = self.necklace_variants.copy()
        self.retired_test_necklaces = set()
        upgraded_necklaces = {variant: donor for donor, variant in self.necklace_variants.items()}
        gear_sets = {row['gear_set_type'] for row in data.tables['excel_affix_gear_set_map_rule']}
        set_fields = {row['name'].removeprefix('affix_'): row['attribute_type']
                      for row in data.tables['excel_battle_attribute_description'] if row['attribute_type'] in gear_sets}
        set_fields['gear_set_coin_master'] = 11009
        original_sets = {
            row['affix_pool_id']: {set_fields[field] for field, value in row['attribute_weight'].items()
                                   if field in set_fields and int(value.get('weight', 0)) > 0}
            for row in data.tables['excel_affix_pool']
        }
        original_sets.update(SET_POOL_MEMBERS)
        value_tables = {}
        for item_id, item in data.items.items():
            if item['item_major_type'] != 'EN_ITEM_MAJOR_TYPE_EQUIPMENT':
                continue
            if item_id in self.necklace_variants:
                continue
            source = data.sources.get(item.get('ue_source_type_id') or item_id, {})
            rarity, item_type = source.get('rarity'), source.get('item_type')
            if item.get('description') == 'Item_CareerGM' and '/ID_Necklace_000' in source.get('ue_path', ''):
                donor = CLASS_NECKLACE_REPLACEMENTS[item_id % 100]
                variant = self.necklace_variants[donor]
                if source['position_cfg'] != data.sources[variant]['position_cfg']:
                    raise ValueError(f'necklace replacement changes grid dimensions: {item_id}')
                self.retired_necklaces[item_id] = variant
                self.retired_test_necklaces.add(item_id)
                continue
            necklace = item_id in upgraded_necklaces
            if rarity in (LEGENDARY, UNIQUE) and item_type in (ARMOR, WEAPON, ACCESSORY):
                template = deepcopy(originals[item_id])
                original = template['data']['affix_value_id']
                offset = NECKLACE_VALUE_OFFSET if necklace else (
                    LEGENDARY_VALUE_OFFSET if rarity == LEGENDARY else UNIQUE_VALUE_OFFSET)
                custom = offset + original
                template['data']['affix_value_id'] = custom
                value_tables[custom] = (original, rarity, necklace)
                reserved = {gear_set for rule in template['data']['random_rule'] for choice in rule['pool_random']
                            for pool_id in choice['affix_pool_id'] for gear_set in original_sets.get(pool_id, ())}
                available = [pool_id for pool_id, members in SET_POOL_MEMBERS.items()
                             if not reserved.intersection(members)]
                bonus_count = 2 if rarity == UNIQUE else 1
                if len(available) < bonus_count:
                    raise ValueError(f'not enough disjoint bonus set pools for equipment {item_id}')
                rules = [minor_rule(pool) for pool in available] if bonus_count == 2 else [minor_rule(*available)]
                self.append_rules(item_id, template, rules)
                self.high_rarity[item_id] = rarity
                self.counts[rarity, item_type] += 1
            elif rarity == EXCEPTIONAL and originals.get(item_id, {}).get('data', {}).get('affix_value_id'):
                template = deepcopy(originals[item_id])
                self.append_rules(item_id, template, [minor_rule(100101), minor_rule(100401, 100402)])
                self.counts[rarity, item_type] += 1
            if necklace or item_id in UNIQUE_ACCESSORY_STATS:
                primary = (LEGENDARY_NECKLACE_STATS[upgraded_necklaces[item_id]] if necklace
                           else UNIQUE_ACCESSORY_STATS[item_id])
                stats = {'first_level_' + primary: 12 if necklace else 14,
                         'first_level_stamina': 6 if necklace else 8}
                index = next(index for index, rule in enumerate(template['data']['fixed_rule'])
                             if rule['affix_type'] == BASIC and rule['location'] == EQUIPPED)
                for name, value in stats.items():
                    self.update_fixed(item_id, index, name, {'fixed_value': value})
                if necklace:
                    self.necklace_stats[item_id] = stats

        self.necklace_equipment = {}
        careers = sorted({row['meta']['career'] for row in data.roles.values()})
        original_equipment = {row['equip_id']: row for row in data.tables['ue_source_equipment']}
        for row in data.tables['ue_source_equipment']:
            if row['equip_id'] not in self.necklace_variants:
                continue
            upgraded = deepcopy(row)
            for gear in upgraded['gear_configs']:
                if not gear['career_restrict']:
                    gear['career_restrict'] = careers
            self.necklace_equipment[row['equip_id']] = upgraded
        if self.necklace_equipment.keys() != self.necklace_variants.keys():
            raise ValueError('missing original legendary necklace equipment configuration')
        self.raise_unique_fixed(data, original_equipment)

        original_values = {}
        for row in data.tables['excel_affix_value_template']:
            original_values.setdefault(row['affix_value_id'], []).append(row)
        if original_values.keys() & value_tables.keys():
            raise ValueError('local equipment value tables conflict with original data')
        self.value_rows = []
        legendary_maximum = {}
        for value_id, (original_id, rarity, necklace) in value_tables.items():
            if rarity != LEGENDARY:
                continue
            for row in original_values[original_id]:
                attribute = row['attribute_type']
                is_gear_set = attribute in gear_sets
                upgraded = upgrade_value_row(row, value_id, 6, (7.0, 8.0), is_gear_set)
                if necklace:
                    upgraded = upgrade_value_row(upgraded, value_id, 6, (8.0, 9.0), is_gear_set, compare_at=6)
                self.value_rows.append(upgraded)
                if not is_gear_set:
                    for affix in upgraded['data']['affix_data']:
                        bounds = affix['rarity_range'].get('6')
                        if bounds and bounds['min_value'] > 0:
                            key = (attribute, affix['affix_type'])
                            legendary_maximum[key] = max(bounds['max_value'], legendary_maximum.get(key, 0))
        for value_id, (original_id, rarity, _) in value_tables.items():
            if rarity != UNIQUE:
                continue
            for row in original_values[original_id]:
                is_gear_set = row['attribute_type'] in gear_sets
                upgraded = upgrade_value_row(row, value_id, 6, (7.0, 8.0), is_gear_set)
                upgraded = upgrade_value_row(upgraded, value_id, 7, (10.0, 11.0), is_gear_set)
                if not is_gear_set:
                    unit = upgraded['data']['unit']
                    for affix in upgraded['data']['affix_data']:
                        ranges = affix['rarity_range']
                        if '7' in ranges:
                            key = (row['attribute_type'], affix['affix_type'])
                            ranges['7'] = interval_above(ranges['7'], legendary_maximum.get(key), unit)
                self.value_rows.append(upgraded)

    def update_fixed(self, item_id, index, name, value):
        template = self.templates[item_id]
        template['data']['fixed_rule'][index]['attribute'][name] = value
        self.fixed_adjustments.setdefault(item_id, {}).setdefault(index, {})[name] = value
        if name in SCORE_FIELDS:
            template['server_affix_score'][SCORE_FIELDS[name]] = int(value['fixed_value'])

    def raise_unique_fixed(self, data, equipment):
        relevant = {}
        for item_id, rarity in self.high_rarity.items():
            item = data.items[item_id]
            source = data.sources[item.get('ue_source_type_id') or item_id]
            equip = equipment.get(source.get('ue_source_equipment_equip_id'), {})
            subtype = equip.get('equip_subtype') or source.get('slot_type')
            for index, rule in enumerate(self.templates[item_id]['data']['fixed_rule']):
                if rule['affix_type'] != BASIC:
                    continue
                for name, value in rule['attribute'].items():
                    if name in ORDINARY_FIXED_FIELDS:
                        relevant[item_id, index, name] = (rarity, (subtype, name), value)
        maxima = {}
        score_maxima = {}
        for rarity, key, value in relevant.values():
            maximum = value['offset']['max_value'] if 'offset' in value else value.get('fixed_value', 0)
            if rarity == LEGENDARY and maximum > 0:
                maxima[key] = max(maximum, maxima.get(key, 0))
                name = key[1]
                if name in SCORE_FIELDS:
                    score_maxima[name] = max(maximum, score_maxima.get(name, 0))
        for (item_id, index, name), (rarity, key, value) in relevant.items():
            maximum = maxima.get(key, score_maxima.get(name))
            if rarity != UNIQUE or maximum is None:
                continue
            if 'offset' in value:
                old = value['offset']
                bounds = interval_above(old, maximum, old['unit'])
                if bounds != old:
                    self.update_fixed(item_id, index, name, {'offset': dict(bounds, unit=old['unit'])})
            elif 0 < value.get('fixed_value', 0) <= maximum:
                self.update_fixed(item_id, index, name, {'fixed_value': maximum + 1})

    def add_dark_gold_armor(self, data, originals):
        # The first shipped legendary model in each slot supplies the same original
        # blueprint and grid dimensions for its distinct dark-gold item variant.
        for item_id, item in data.items.items():
            if item_id % 1000 != 0:
                continue
            source = data.sources.get(item.get('ue_source_type_id') or item_id, {})
            if source.get('rarity') != LEGENDARY or source.get('item_type') != ARMOR:
                continue
            variant_id = item_id + 1000
            template = self.clone_variant(data, originals, item_id, variant_id, UNIQUE)
            template['server_affix_score']['defend_score'] = 6
            fixed = next(rule['attribute'] for rule in template['data']['fixed_rule']
                         if rule['affix_type'] == BASIC and rule['location'] == EQUIPPED)
            fixed['defend_def_gear_level']['fixed_value'] = 6

    def clone_variant(self, data, originals, item_id, variant_id, rarity):
        if variant_id in data.items or variant_id in data.sources or variant_id in self.added_items:
            raise ValueError(f'local equipment variant ID conflicts with original data: {variant_id}')
        item = data.items[item_id]
        source = data.sources[item.get('ue_source_type_id') or item_id]
        self.added_items[variant_id] = dict(item, type_id=variant_id, ue_source_type_id=variant_id)
        self.added_sources[variant_id] = dict(source, type_id=variant_id, rarity=rarity)
        template = deepcopy(originals[item_id])
        template['item_id'] = variant_id
        originals[variant_id] = template
        return template

    def append_rules(self, item_id, template, rules):
        current = template['data']['random_rule']
        # The native roll generator stops at the first empty placeholder. New
        # rolls precede that marker so every original populated index survives.
        start = len(current)
        while start and not current[start - 1].get('pool_random'):
            start -= 1
        self.bonus_rule_start[item_id] = start
        self.bonus_rule_count[item_id] = len(rules)
        current[start:start] = rules
        self.templates[item_id] = template
