"""Local seasonal affix pools and their original item value ranges."""
from collections import defaultdict
import random

from equipment_balance import MINOR, SET_POOL_MEMBERS


# The public client contains the seasonal rule IDs and value ranges, but does
# not ship these seasonal pool rows. These are local subsets of the named
# categories. Every choice must also have an original value for the item.
LOCAL_POOL_ATTRIBUTES = {
    100101: (1001, 1002, 1003, 1004, 1005, 1006),
    100201: (1102, 1103),
    100202: (1104, 1105),
    100203: (1106, 1107),
    100301: (1108,),  # Fire
    100302: (1110,),  # Lightning
    100303: (1109,),  # Frost
    100304: (1111,),  # Divine
    100305: (1112,),  # Shadow
    100401: (2002, 2003),
    100402: (2007, 2008),
    100501: (1113, 1121),
    100502: (1114, 1115, 1116, 1117, 1118, 1119),
    100503: (11001, 11002, 11004, 11007, 11008),
    100601: (5002, 5003, 5004, 5006, 5105),
    100602: (4002, 4008, 4010),
    100701: (2005,),
    100801: (11009,),  # Coin Master, from the original green preview.
    100901: (4003, 4004, 6002, 6003, 6103, 6104, 6105, 6106, 6206),
    100902: (3002, 6001, 6101, 6102, 6201, 6202, 6203, 6205, 6208),
    101301: (11003, 11005, 11006),
    **SET_POOL_MEMBERS,
}
class TradeAffixes:
    def __init__(self, game_data):
        self.game_data = game_data
        tables = game_data.tables
        self.templates = {row['item_id']: row['data'] for row in tables['excel_affix_template']}
        self.balance = game_data.equipment_balance
        self.templates.update({item_id: row['data'] for item_id, row in self.balance.templates.items()})
        self.ranges = defaultdict(set)
        for row in (*tables['excel_affix_value_template'], *self.balance.value_rows):
            for affix in row['data']['affix_data']:
                if affix['affix_type'] == MINOR:
                    for rarity in affix['rarity_range']:
                        self.ranges[row['affix_value_id'], int(rarity)].add(row['attribute_type'])
        self.counts = defaultdict(list)
        for row in tables['excel_affix_count_random_pool']:
            self.counts[row['affix_count_pool_id']].append((row['count'], int(row['random_weight'])))
        self.gear_sets = {row['gear_set_type'] for row in tables['excel_affix_gear_set_map_rule']}
        self.minor = game_data.enum('EnBattleAffixType', MINOR)

    def normalize_equipment(self, item):
        item_id = self.balance.retired_necklaces.get(item.basic.type_id, item.basic.type_id)
        if item.basic.type_id != item_id:
            item.basic.type_id = item_id
        start = self.balance.bonus_rule_start.get(item_id)
        if start is None:
            return
        source_id = self.game_data.items[item_id].get('ue_source_type_id') or item_id
        rarity = self.game_data.enum('EnItemRarity', self.game_data.sources[source_id]['rarity'])
        self.roll(item, rarity, random.Random(item.basic.guid), start)

    def roll(self, item, rarity, rng, start=0):
        template = self.templates.get(item.basic.type_id)
        if not template:
            return
        ranges = self.ranges[template['affix_value_id'], rarity]
        attributes = {key for group in item.data.affix_data for key in group.attribute_random}
        gear_sets = {key for group in item.data.affix_data for key in group.gear_set_random}
        occupied = {roll.group_index for group in item.data.affix_data if group.affix_type == self.minor
                    for field in ('attribute_random', 'ability_set_random', 'gem_slot_random', 'gear_set_random')
                    for value in getattr(group, field).values() for roll in value.random_data}
        for index, rule in enumerate(template['random_rule'][start:], start + 1):
            if rule['affix_type'] != MINOR or index in occupied:
                continue
            choices = sorted({attribute for pool in rule['pool_random'] for pool_id in pool['affix_pool_id']
                              for attribute in LOCAL_POOL_ATTRIBUTES.get(pool_id, ())}
                             & ranges - attributes - gear_sets)
            if not choices:
                continue
            counts, weights = zip(*self.counts[rule['affix_count_pool_id']])
            count = min(len(choices), rng.choices(counts, weights)[0])
            if not count:
                continue
            location = self.game_data.enum('EnBattleAffixEffectiveLocation', rule['location'])
            group = item.data.affix_data.add(affix_type=self.minor, location=location)
            for attribute in rng.sample(choices, count):
                if attribute in self.gear_sets:
                    random = group.gear_set_random[attribute].random_data.add()
                    gear_sets.add(attribute)
                else:
                    random = group.attribute_random[attribute].random_data.add()
                    random.affix_pool_value = True
                    attributes.add(attribute)
                random.random_level = rng.randint(1, 10000)
                random.group_index = index
