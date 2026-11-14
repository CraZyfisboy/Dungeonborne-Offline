"""Build and independently read back an isolated native loot-configuration pak."""
from pathlib import Path
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys

NATIVE = Path(__file__).resolve().parents[1]
PROJECT = NATIVE.parent
sys.path[:0] = [str(NATIVE / 'backend'), str(NATIVE / 'tools')]
from protocol import ClientProtocol
from game_data import ADVANCED_POTION_PRODUCTS, BANDAGE_IDS, GameData, LOCAL_MERCHANT_TABS
from trade_armor import additional_trade_rows
from loot_rules import LootRules, compile_native_loot
from state import StateStore
from inventory import CONSUMABLE_STACK_LIMIT
from affixes import LOCAL_POOL_ATTRIBUTES
from equipment_balance import ACCESSORY, ARMOR, BASIC, EXCEPTIONAL, LEGENDARY, SCORE_FIELDS, UNIQUE, WEAPON
from prepare_native_probe import pak
from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory


def native_affix_config(data, raw, pool, envelope):
    def read(name):
        table = envelope.FromString((raw / f'ExcelConfig/{name}.bytes').read_bytes())
        return table, message_factory.GetMessageClass(pool.FindMessageTypeByName(table.data_message_type))

    balance = data.equipment_balance
    pools, pool_type = read('affix_pool')
    existing = {pool_type.FromString(block).affix_pool_id for block in pools.data_block}
    configured = {row['item_id']: row for row in data.tables['excel_affix_template']}
    configured.update(balance.templates)
    referenced = {pool_id for row in configured.values() for rule in row['data']['random_rule']
                  for choice in rule['pool_random'] for pool_id in choice['affix_pool_id']}
    missing = referenced - existing
    if missing - LOCAL_POOL_ATTRIBUTES.keys():
        raise ValueError('unmapped native seasonal affix pools')
    fields = {row['attribute_type']: row['name'].removeprefix('affix_')
              for row in data.tables['excel_battle_attribute_description']}
    fields.update({6201: 'trigger_effect_heal_on_open_chest', 6202: 'trigger_effect_magic_on_open_chest',
                   6203: 'trigger_effect_gain_gold_on_open_chest', 11009: 'gear_set_coin_master'})
    for pool_id in sorted(missing):
        row = pool_type(affix_pool_id=pool_id)
        for attribute in LOCAL_POOL_ATTRIBUTES[pool_id]:
            getattr(row.attribute_weight, fields[attribute]).weight = 1
        pools.data_block.append(row.SerializeToString(deterministic=True))

    templates, template_type = read('affix_template')
    low_rarity = {'EN_ITEM_RARITY_ROTTED', 'EN_ITEM_RARITY_NORMAL'}
    basic = data.enum('EnBattleAffixType', BASIC)
    floored = set()
    processed = set()
    for index, block in enumerate(templates.data_block):
        row = template_type.FromString(block)
        item_id = row.item_id
        if item_id in balance.templates:
            processed.add(item_id)
            updated = balance.templates[item_id]['data']
            if item_id in balance.bonus_rule_start:
                row.data.affix_value_id = updated['affix_value_id']
                start = balance.bonus_rule_start[item_id]
                finish = start + balance.bonus_rule_count[item_id]
                trailing = list(row.data.random_rule[start:])
                del row.data.random_rule[start:]
                for rule in updated['random_rule'][start:finish]:
                    json_format.ParseDict(rule, row.data.random_rule.add())
                row.data.random_rule.extend(trailing)
            for rule_index, adjustments in balance.fixed_adjustments.get(item_id, {}).items():
                fixed = row.data.fixed_rule[rule_index].attribute
                for name, value in adjustments.items():
                    json_format.ParseDict(value, getattr(fixed, name))
                    if name in SCORE_FIELDS:
                        score = SCORE_FIELDS[name]
                        setattr(row.server_affix_score, score, balance.templates[item_id]['server_affix_score'][score])
            templates.data_block[index] = row.SerializeToString(deterministic=True)
            continue
        item = data.items.get(row.item_id, {})
        source = data.sources.get(item.get('ue_source_type_id') or row.item_id, {})
        if (item.get('item_major_type') != 'EN_ITEM_MAJOR_TYPE_EQUIPMENT'
                or source.get('rarity') not in low_rarity):
            continue
        for attribute, score in SCORE_FIELDS.items():
            if not getattr(row.affix_display, f'display_{attribute}'):
                continue
            for rule in row.data.fixed_rule:
                if rule.affix_type == basic and rule.attribute.HasField(attribute):
                    value = getattr(rule.attribute, attribute)
                    value.fixed_value = max(value.fixed_value, 1)
                    setattr(row.server_affix_score, score, max(getattr(row.server_affix_score, score), 1))
                    floored.add(row.item_id)
                    break
            else:
                raise ValueError(f'original displayed gear level lacks a basic rule: {row.item_id}')
        if row.item_id in floored:
            templates.data_block[index] = row.SerializeToString(deterministic=True)

    for item_id in sorted(balance.templates.keys() - processed):
        if item_id not in balance.added_items:
            raise ValueError(f'missing original armor or weapon template: {item_id}')
        row = json_format.ParseDict(balance.templates[item_id], template_type())
        templates.data_block.append(row.SerializeToString(deterministic=True))

    values, value_type = read('affix_value_template')
    for definition in balance.value_rows:
        row = json_format.ParseDict(definition, value_type())
        values.data_block.append(row.SerializeToString(deterministic=True))

    items, item_type = read('item_type')
    for item_id, variant in sorted(balance.added_items.items()):
        row = json_format.ParseDict(variant, item_type())
        if row.type_id != item_id or row.ue_source_type_id != item_id:
            raise ValueError(f'invalid new high-rarity equipment row: {item_id}')
        items.data_block.append(row.SerializeToString(deterministic=True))

    tables = {'affix_pool': pools, 'affix_template': templates,
              'affix_value_template': values, 'item_type': items}
    for table in tables.values():
        table.header.count = len(table.data_block)
        table.header.hash_code = 'sha256:' + hashlib.sha256(b''.join(table.data_block)).hexdigest()
    return {f'ExcelConfig/{name}.bytes': table.SerializeToString(deterministic=True)
            for name, table in tables.items()}, dict(
                local_affix_pools=len(missing), basic_gear_items=len(floored),
                legendary_armor=balance.counts[LEGENDARY, ARMOR], legendary_weapons=balance.counts[LEGENDARY, WEAPON],
                unique_armor=balance.counts[UNIQUE, ARMOR],
                unique_armor_variants=len(balance.added_items)-len(balance.necklace_variants),
                unique_weapons=balance.counts[UNIQUE, WEAPON],
                legendary_accessories=balance.counts[LEGENDARY, ACCESSORY],
                unique_accessories=balance.counts[UNIQUE, ACCESSORY],
                exceptional_armor=balance.counts[EXCEPTIONAL, ARMOR],
                legendary_necklaces=len(balance.necklace_stats),
                retired_test_necklaces=len(balance.retired_test_necklaces))


def lobby_config(data):
    """Use the original storage limits' integer representation, without a game cap."""
    raw = PROJECT / 'analysis/raw-config/Dungeonborne/Content/AdditionalData'
    pool = descriptor_pool.DescriptorPool()
    for descriptor in descriptor_pb2.FileDescriptorSet.FromString((raw / 'Protocol/Configure.pb').read_bytes()).file:
        pool.Add(descriptor)
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName('org.xresloader.pb.xresloader_datablocks'))
    table = cls.FromString((NATIVE / 'payload/Dungeonborne/Content/AdditionalData/ExcelConfig/const.bytes').read_bytes())
    row_class = message_factory.GetMessageClass(pool.FindMessageTypeByName(table.data_message_type))
    requested = {'inventory_max_page', 'gear_inventory_max_count'}
    changed = {}
    for index, block in enumerate(table.data_block):
        row = row_class.FromString(block)
        if row.key in requested:
            changed[row.key] = row.value
            row.value = '2147483647'
            table.data_block[index] = row.SerializeToString(deterministic=True)
    if changed.keys() != requested:
        raise ValueError('missing original warehouse limits')
    table.header.hash_code = 'sha256:' + hashlib.sha256(b''.join(table.data_block)).hexdigest()
    merchant=cls.FromString((raw/'ExcelConfig/market_info.bytes').read_bytes())
    row_class=message_factory.GetMessageClass(pool.FindMessageTypeByName(merchant.data_message_type))
    found=False
    for index,block in enumerate(merchant.data_block):
        row=row_class.FromString(block)
        if row.market_id==101:
            del row.data.market_tab_ids[:]
            row.data.market_tab_ids.extend(LOCAL_MERCHANT_TABS)
            merchant.data_block[index]=row.SerializeToString(deterministic=True)
            found=True
    if not found:
        raise ValueError('missing original merchant')
    merchant.header.hash_code='sha256:'+hashlib.sha256(b''.join(merchant.data_block)).hexdigest()
    products = cls.FromString((raw / 'ExcelConfig/market_product_item.bytes').read_bytes())
    row_class = message_factory.GetMessageClass(pool.FindMessageTypeByName(products.data_message_type))
    advanced = set()
    for index, block in enumerate(products.data_block):
        row = row_class.FromString(block)
        if row.market_tab_id not in data.merchant_tabs:
            continue
        product = row.product
        item_id = ADVANCED_POTION_PRODUCTS.get(row.product_id)
        if item_id is not None:
            product.reward_items[0].type_id = item_id
            product.is_on_sale = True
            product.refresh_count = 999
            product.random_weight = product.refresh_pool_id = row.refresh_pool_id = 0
            advanced.add(row.product_id)
        price = data.merchant_potion_price(row.product_id)
        if price is not None and product.is_on_sale:
            del product.cost_options[:]
            product.cost_options.add().cost_items.add(cost_type=1001, cost_price=price, origin_price=price)
            products.data_block[index] = row.SerializeToString(deterministic=True)
    if advanced != ADVANCED_POTION_PRODUCTS.keys():
        raise ValueError('missing original advanced potion listings')
    products.header.hash_code = 'sha256:' + hashlib.sha256(b''.join(products.data_block)).hexdigest()

    sources = cls.FromString((raw / 'UeSourceConfig/UeSource_ItemType.bytes').read_bytes())
    row_class = message_factory.GetMessageClass(pool.FindMessageTypeByName(sources.data_message_type))
    patched = set()
    for index, block in enumerate(sources.data_block):
        row = row_class.FromString(block)
        if row.type_id in data.extended_stack_ids:
            row.position_cfg.accumulation_limit = CONSUMABLE_STACK_LIMIT
            sources.data_block[index] = row.SerializeToString(deterministic=True)
            patched.add(row.type_id)
    if patched != data.extended_stack_ids:
        raise ValueError('missing original potion or bandage stack configuration')
    for item_id, source in sorted(data.equipment_balance.added_sources.items()):
        row = json_format.ParseDict(source, row_class())
        if row.type_id != item_id:
            raise ValueError(f'invalid new high-rarity equipment source: {item_id}')
        sources.data_block.append(row.SerializeToString(deterministic=True))
    sources.header.count = len(sources.data_block)
    sources.header.hash_code = 'sha256:' + hashlib.sha256(b''.join(sources.data_block)).hexdigest()
    equipment = cls.FromString((raw / 'UeSourceConfig/UeSource_Equipment.bytes').read_bytes())
    row_class = message_factory.GetMessageClass(pool.FindMessageTypeByName(equipment.data_message_type))
    equipment_overrides = data.equipment_balance.necklace_equipment
    for index, block in enumerate(equipment.data_block):
        row = row_class.FromString(block)
        if row.equip_id not in equipment_overrides:
            continue
        for gear, upgraded in zip(row.gear_configs, equipment_overrides[row.equip_id]['gear_configs'], strict=True):
            del gear.career_restrict[:]
            json_format.ParseDict({'career_restrict': upgraded['career_restrict']}, gear)
        equipment.data_block[index] = row.SerializeToString(deterministic=True)
    equipment.header.hash_code = 'sha256:' + hashlib.sha256(b''.join(equipment.data_block)).hexdigest()
    item_strings_path = 'StringTable/ItemStringTable.csv'
    item_strings = (raw / item_strings_path).read_bytes()
    name_rows = {data.items[item_id]['name']: name
                 for item_id, name in data.equipment_balance.necklace_display_names.items()}
    existing_keys = {row['Key'] for row in csv.DictReader(io.StringIO(item_strings.decode('utf-8-sig'), newline=''))}
    if existing_keys & name_rows.keys() or not item_strings.endswith(b'\r\n'):
        raise ValueError('original item string table is incompatible with local necklace names')
    additions = io.StringIO(newline='')
    csv.writer(additions, quoting=csv.QUOTE_ALL, lineterminator='\r\n').writerows(sorted(name_rows.items()))
    item_strings += additions.getvalue().encode('utf8')
    trade = cls.FromString((raw / 'ExcelConfig/trade_commodity.bytes').read_bytes())
    trade_class = message_factory.GetMessageClass(pool.FindMessageTypeByName(trade.data_message_type))
    equipment_listings = additional_trade_rows(data)
    for definition in equipment_listings:
        row = json_format.ParseDict(definition, trade_class())
        trade.data_block.append(row.SerializeToString(deterministic=True))
    trade.header.count = len(trade.data_block)
    trade.header.hash_code = 'sha256:' + hashlib.sha256(b''.join(trade.data_block)).hexdigest()
    affixes, counts = native_affix_config(data, raw, pool, cls)
    counts['additional_equipment_trade_rows'] = len(equipment_listings)
    return {'ExcelConfig/const.bytes': table.SerializeToString(deterministic=True),
            'ExcelConfig/market_info.bytes': merchant.SerializeToString(deterministic=True),
            'ExcelConfig/market_product_item.bytes': products.SerializeToString(deterministic=True),
            'ExcelConfig/trade_commodity.bytes': trade.SerializeToString(deterministic=True),
            'UeSourceConfig/UeSource_ItemType.bytes': sources.SerializeToString(deterministic=True),
            'UeSourceConfig/UeSource_Equipment.bytes': equipment.SerializeToString(deterministic=True),
            item_strings_path: item_strings,
            **affixes}, changed, counts


def build(save, staging):
    protocol = ClientProtocol()
    data = GameData(protocol)
    rules = LootRules(data)
    store = StateStore(save, protocol, data)
    try:
        with store.transaction() as transaction:
            snapshot = rules.snapshot(transaction)
    finally:
        store.close()
    payloads, counts = compile_native_loot(snapshot, rules)
    lobby, original_limits, affix_counts = lobby_config(data)
    counts.update(affix_counts)
    for name, table in lobby.items():
        payloads[f'Dungeonborne/Content/AdditionalData/{name}'] = table
    consumable_definitions = [(item_id, data.sources[item_id]['ue_path']) for item_id in sorted(data.extended_stack_ids)]
    if any(not re.fullmatch(r'/Game/[\w/]+\.[\w]+', path, flags=re.ASCII) for _, path in consumable_definitions):
        raise ValueError('invalid original potion or bandage definition path')
    payloads['Dungeonborne/Content/Script/OfflineProbe/PotionDefinitions.lua'] = (
        f'return {{limit={CONSUMABLE_STACK_LIMIT}, paths={{\n'
        + ''.join(f'    [{item_id}]="{path}",\n' for item_id, path in consumable_definitions) + '}}\n').encode('ascii')
    snapshot_bytes = json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode('utf8')
    package_bytes = pak(payloads)
    fingerprint = hashlib.sha256(snapshot_bytes + hashlib.sha256(package_bytes).digest()).hexdigest()
    directory = NATIVE / 'artifacts/loot-config' / fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    package = directory / 'pakchunk10002-OfflineLoot_P.pak'
    if package.exists() and package.read_bytes() != package_bytes:
        raise ValueError('same loot configuration produced different bytes')
    package.write_bytes(package_bytes)
    (directory / 'settings.json').write_bytes(snapshot_bytes)
    probe_package = staging / 'Dungeonborne/Content/Paks/pakchunk10001-OfflineLootTest_P.pak'
    if probe_package.exists():
        probe_package.unlink()  # Remove only our superseded experiment, retained in artifacts.
    shutil.copy2(package, staging / 'Dungeonborne/Content/Paks' / package.name)
    if (NATIVE / 'tools/release/resource-manifest.json').is_file():
        from release.resources import verify_patch
        verify_patch(staging / 'Dungeonborne/Content/Paks' / package.name, payloads)
    else:
        output = NATIVE / 'analysis/loot-config-readback' / fingerprint
        environment = os.environ.copy()
        environment.update(DB_PROJECT_ROOT=str(PROJECT), DB_READ_INSTALL=str(staging),
                           DOTNET_CLI_HOME=str(PROJECT / 'tools/dotnet-home'))
        payload_pattern = '^(?:' + '|'.join(re.escape(path) for path in sorted(payloads)) + ')$'
        command = [str(PROJECT / 'tools/dotnet/dotnet.exe'), str(NATIVE / 'tools/ArchiveReader/ArchiveReader.dll'),
                   'raw', payload_pattern, str(output), '-', 'UE5_1']
        result = subprocess.run(command, env=environment, cwd=PROJECT, capture_output=True, text=True,
                                encoding='utf8', errors='replace')
        (directory / 'readback.log').write_text(result.stdout + result.stderr, encoding='utf8')
        if result.returncode or any((output / path).read_bytes() != block for path, block in payloads.items()):
            raise RuntimeError('native loot configuration independent readback failed')
    report = dict(configuration_sha256=fingerprint, package=str(package),
                  package_sha256=hashlib.sha256(package_bytes).hexdigest(),
                  independently_read_back=True, save=str(save), counts=counts,
                  multiplier=snapshot['multiplier'], difficulty_map=rules.presets['difficulty_map'],
                  original_warehouse_limits=original_limits,merchant_tabs=LOCAL_MERCHANT_TABS,
                  potion_stack_limit=CONSUMABLE_STACK_LIMIT,potion_type_ids=sorted(data.potion_ids),
                  bandage_type_ids=sorted(BANDAGE_IDS),advanced_potion_products=ADVANCED_POTION_PRODUCTS)
    (directory / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    (NATIVE / 'reports/loot-config-preparation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', type=Path, required=True)
    parser.add_argument('--runtime-name', choices=['runtime', 'runtime-headless'], default='runtime-headless')
    options = parser.parse_args()
    if not options.save.resolve().is_relative_to(NATIVE):
        parser.error('save must remain in the native project')
    count = subprocess.run(['powershell', '-NoProfile', '-Command',
                            '@(Get-Process -Name Dungeonborne -ErrorAction SilentlyContinue).Count'],
                           capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if count.stdout.strip() != '0':
        raise RuntimeError('close the test game before installing a new native configuration pak')
    report = build(options.save.resolve(), NATIVE / options.runtime_name)
    print(json.dumps({'counts':report['counts'], 'configuration_sha256':report['configuration_sha256'],
                      'independently_read_back':True}))
