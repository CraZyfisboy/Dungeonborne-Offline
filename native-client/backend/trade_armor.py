"""Additional local equipment listings using the original sale and auction rules."""
from equipment_balance import ARMOR, EXCEPTIONAL, LEGENDARY, UNIQUE


def armor_trade_rows(data):
    original = data.tables['excel_trade_commodity']
    existing = {(row['market_id'], row['id']) for row in original}
    epic = {}
    exceptional_prices = {}
    for row in original:
        source = data.sources.get(row['item_id'], {})
        if source.get('item_type') != ARMOR or not row['is_on_shelf']:
            continue
        key = (row['market_id'], source['slot_type'])
        if source.get('rarity') == 'EN_ITEM_RARITY_EPIC':
            if key not in epic or row['item_id'] < epic[key]['item_id']:
                epic[key] = row
        elif source.get('rarity') == EXCEPTIONAL:
            exceptional_prices[key] = max(row['price_def'], exceptional_prices.get(key, 0))
    result = []
    for type_id, item in sorted(data.items.items()):
        source = data.sources.get(item.get('ue_source_type_id') or type_id, {})
        rarity = source.get('rarity')
        if source.get('item_type') != ARMOR or rarity not in (LEGENDARY, UNIQUE):
            continue
        for market in (1, 2):
            product_id = type_id * 10
            if (market, product_id) in existing:
                continue
            key = (market, source['slot_type'])
            donor = epic[key]
            exceptional = exceptional_prices.get(key, 0)
            if rarity == LEGENDARY:
                price = max(donor['price_def'] * 2, (exceptional * 5 + 3) // 4)
            else:
                price = max(donor['price_def'] * 3, (exceptional * 8 + 4) // 5)
            result.append(dict(donor, id=product_id, item_id=type_id, price_def=price))
    return result


def additional_trade_rows(data):
    armor = armor_trade_rows(data)
    original = data.tables['excel_trade_commodity']
    existing = {(row['market_id'], row['id']) for row in (*original, *armor)}
    # High-rarity accessories already on the market supply the rules for each
    # market. The unlisted equipment supplies its own authored standard price.
    comparables = {LEGENDARY: 185015000, UNIQUE: 185015001}
    donors = {(r['market_id'], r['item_id']): r for r in original if r['is_on_shelf']}
    balance = data.equipment_balance
    reserved = {r['inst_id'] for r in data.tables['excel_gear_heirloom_guide']}
    reserved.update(r['id'] for r in data.tables['excel_mithril_force_item'])
    reserved.update(balance.retired_necklaces)
    result = list(armor)
    for item_id, rarity in sorted(balance.high_rarity.items()):
        item = data.items[item_id]
        price = item['item_quest_value']
        if (price <= 0 or item_id in reserved or item['is_forbidden_bring_into_battle']
                or 'HEIRLOOM' in item['skin_third_sort']):
            continue
        product_id = item_id * 10
        for market in (1, 2):
            if (market, product_id) in existing:
                continue
            donor = donors[market, comparables[rarity]]
            result.append(dict(donor, id=product_id, item_id=item_id, price_def=price))
    return result
