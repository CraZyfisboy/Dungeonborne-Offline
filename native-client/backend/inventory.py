"""Native DItem placement, preserving complete protobuf item data."""
from __future__ import annotations

from dataclasses import dataclass


WAREHOUSE, BACKPACK, WORN, POUCH = 1, 2, 3, 4
AUTO_WAREHOUSE, AUTO_BACKPACK = 101, 102
WEAPON_PAIRS = {1: 2, 2: 1, 3: 4, 4: 3}
RIGHT_WEAPON_SLOTS = (1, 3)
RING_SLOTS = frozenset((17, 18))
CONSUMABLE_STACK_LIMIT = 999
# Level one has one original-size page. Each additional page doubles the price.
BACKPACK_UPGRADE_COSTS = (1000, 2000, 4000, 8000, 16000, 32000, 64000, 128000, 256000)


@dataclass
class Storage:
    kind: int
    guid: int
    holder: object
    role: object = None

    def entries(self):
        if self.kind == WAREHOUSE:
            return [item for group in self.holder.item_group for item in group.element]
        return list(self.holder.items)

    def append(self, item):
        if self.kind == WAREHOUSE:
            if not self.holder.item_group:
                self.holder.item_group.add()
            self.holder.item_group[0].element.add().CopyFrom(item)
        else:
            self.holder.items.add().CopyFrom(item)

    def remove(self, guid):
        groups = [g.element for g in self.holder.item_group] if self.kind == WAREHOUSE else [self.holder.items]
        for group in groups:
            for index, item in enumerate(group):
                if item.basic.guid == guid:
                    del group[index]
                    return
        raise ValueError('物品已移走，请重新打开背包')


class Inventory:
    def __init__(self, protocol, game_data):
        self.protocol, self.game_data = protocol, game_data
        self.gear = {row['equip_id']: row for row in game_data.tables['ue_source_equipment']}
        self.wear_slots = protocol.pool.FindEnumTypeByName('tgf.EnItemWearingPart').values_by_name
        self.wear_slots_to_numbers = {s.number for s in self.wear_slots.values() if s.number}

    def dimensions(self, type_id):
        source = self.game_data.sources.get(type_id)
        if source is None:
            item = self.game_data.items.get(type_id, {})
            source = self.game_data.sources.get(item.get('ue_source_type_id', 0))
        if source is None:
            raise ValueError('missing original item dimensions: ' + str(type_id))
        config = source['position_cfg']
        width, height, stack = config['column_size'], config['row_size'], config['accumulation_limit']
        if source['type_id'] in self.game_data.extended_stack_ids:
            stack = CONSUMABLE_STACK_LIMIT
        if min(width, height, stack) < 1:
            raise ValueError('invalid original item dimensions')
        return width, height, stack

    def equipment(self, type_id):
        source = self.game_data.sources.get(type_id)
        if source is None:
            source = self.game_data.sources.get(self.game_data.items.get(type_id, {}).get('ue_source_type_id', 0), {})
        return self.gear.get(source.get('ue_source_equipment_equip_id', 0), {})

    def two_handed(self, item):
        return any(weapon['weapon_equip_type'] == 'EN_WEAPON_EQUIP_TYPE_BOTHHAND'
                   for weapon in self.equipment(item.basic.type_id).get('weapon_configs', ()))

    def compatible_slot(self, role, type_id, slot):
        career = self.protocol.pool.FindEnumTypeByName('tgf.EnRoleCareerType').values_by_number[role.basic.meta.career].name
        equip = self.equipment(type_id)
        for gear in equip.get('gear_configs', ()):
            if (not gear['career_restrict'] or career in gear['career_restrict']) and any(
                slot == (s if isinstance(s, int) else self.wear_slots[s].number)
                for s in gear['available_equip_slot']
            ):
                return True
        name = self.protocol.pool.FindEnumTypeByName('tgf.EnItemWearingPart').values_by_number.get(slot)
        side = 'RIGHT' if name and name.name.endswith('RIGHTWEAPON') else 'LEFT' if name and name.name.endswith('LEFTWEAPON') else ''
        for weapon in equip.get('weapon_configs', ()):
            equip_type = weapon['weapon_equip_type'].removeprefix('EN_WEAPON_EQUIP_TYPE_')
            if equip_type == 'CONSUMABLE' and slot in (41, 42, 43):
                return True
            if equip_type == 'THROWABLE' and slot == 51:
                return True
            if not side or career not in weapon['career_restrict']:
                continue
            if (equip_type == 'BOTHHAND' and side == 'RIGHT'
                or equip_type == 'ONLY' + side or equip_type in ('ANYHAND', 'BOTH', 'LEFTRIGHT')
                or weapon['can_equip_double'] and equip_type == 'ONLYRIGHT'):
                return True
        return False

    @staticmethod
    def items(profile):
        for page in profile.user_invetory.inventory_pages:
            for group in page.item_group:
                yield from group.element
        for role in profile.user_role:
            yield from role.deploy_role.backpack.items
            yield from role.deploy_role.wearing.items
            yield from role.deploy_role.small_backpack.items

    def free_position(self, page, items, width, height, page_rows=None):
        # In the original FIntPoint and DItemPosition, X is row and Y is column.
        if width > page.column_size or height > page.row_size:
            return None
        occupied = set()
        for item in items:
            item_width, item_height, _ = self.dimensions(item.basic.type_id)
            for x in range(item.basic.position.x, item.basic.position.x + item_height):
                for y in range(item.basic.position.y, item.basic.position.y + item_width):
                    occupied.add((x, y))
        for x in range(page.row_size - height + 1):
            if page_rows and (x % page_rows) + height > page_rows:
                continue
            for y in range(page.column_size - width + 1):
                if all((xx, yy) not in occupied for xx in range(x, x + height) for yy in range(y, y + width)):
                    return x, y
        return None

    @property
    def bag_page_rows(self):
        return int(self.game_data.constants['role_backpack_default_row_size'])

    def add_page(self, transaction):
        pages = transaction.profile.user_invetory.inventory_pages
        return pages.add(page_id=max((p.page_id for p in pages), default=-1) + 1,
                         row_size=int(self.game_data.constants['inventory_default_row_size']),
                         column_size=int(self.game_data.constants['inventory_defualt_column_size']))

    def storage(self, tx, kind, guid):
        if kind == WAREHOUSE:
            page = next((p for p in tx.profile.user_invetory.inventory_pages if p.page_id == guid), None)
            if page is not None:
                return Storage(kind, guid, page)
        elif kind in (BACKPACK, WORN, POUCH):
            role = next((r for r in tx.profile.user_role if r.basic.role_guid == guid), None)
            if role is not None:
                holder = getattr(role.deploy_role, {BACKPACK:'backpack', WORN:'wearing', POUCH:'small_backpack'}[kind])
                return Storage(kind, guid, holder, role)
        raise ValueError('找不到指定的仓库页或角色')

    def locate(self, tx, basic):
        # The native client sometimes supplies just the item GUID. Never trust
        # its item bytes or quantity; only move data from the authoritative save.
        if not 0 < basic.guid < 2**31:
            raise ValueError('物品编号无效')
        found = next((i for i in self.items(tx.profile) if i.basic.guid == basic.guid), None)
        if found is None or (basic.type_id and basic.type_id != found.basic.type_id):
            raise ValueError('物品不在本地仓库或角色背包里')
        p = found.basic.position
        if basic.HasField('position'):
            claim = basic.position
            if (claim.type, claim.guid, claim.x, claim.y) != (p.type, p.guid, p.x, p.y):
                raise ValueError('物品已移动，请刷新物品栏')
        return self.storage(tx, p.type, p.guid), self.copy(found)

    def copy(self, item):
        result = self.protocol.message('tgf.DItem')
        result.CopyFrom(item)
        return result

    def collisions(self, target, item, x, y, ignore=()):
        width, height, _ = self.dimensions(item.basic.type_id)
        if target.kind == WORN:
            if y != 0 or not self.compatible_slot(target.role, item.basic.type_id, x):
                raise ValueError('原版角色无法在该位置装备这件物品')
            return [e for e in target.entries() if e.basic.guid not in ignore and e.basic.position.x == x]
        if min(x, y) < 0 or x + height > target.holder.row_size or y + width > target.holder.column_size:
            raise ValueError('物品超出格子范围')
        if target.kind == BACKPACK and x % self.bag_page_rows + height > self.bag_page_rows:
            raise ValueError('物品不能跨越两个地牢背包页')
        overlap = []
        for existing in target.entries():
            if existing.basic.guid in ignore:
                continue
            ew, eh, _ = self.dimensions(existing.basic.type_id)
            pos = existing.basic.position
            if x < pos.x + eh and pos.x < x + height and y < pos.y + ew and pos.y < y + width:
                overlap.append(existing)
        return overlap

    def resolve_goal(self, tx, goal, item, ignore=()):
        kind, guid, x, y = goal.type, goal.guid, goal.x, goal.y
        if kind in (0, AUTO_WAREHOUSE, AUTO_BACKPACK):
            return self.auto_target(tx, BACKPACK if kind == AUTO_BACKPACK else WAREHOUSE, guid, item, ignore)
        return self.storage(tx, kind, guid), x, y

    def auto_target(self, tx, kind, guid, item, ignore=()):
        width, height, _ = self.dimensions(item.basic.type_id)
        if kind == WAREHOUSE:
            pages = sorted(tx.profile.user_invetory.inventory_pages, key=lambda p:(p.page_id != guid, p.page_id))
            for page in pages:
                target = Storage(kind, page.page_id, page)
                pos = self.free_position(page, (i for i in target.entries() if i.basic.guid not in ignore), width, height)
                if pos is not None:
                    return target, *pos
            page = self.add_page(tx)
            if height > page.row_size or width > page.column_size:
                raise ValueError('物品无法放进一页仓库')
            return Storage(kind, page.page_id, page), 0, 0
        target = self.storage(tx, kind, guid or tx.profile.user_hall_setting.role_guid)
        pos = self.free_position(target.holder, (i for i in target.entries() if i.basic.guid not in ignore),
                                 width, height, self.bag_page_rows if kind == BACKPACK else None)
        if pos is None:
            raise ValueError('地牢背包没有足够的空间')
        return target, *pos

    @staticmethod
    def position(item, target, x, y):
        p = item.basic.position
        # Keep unknown fields when moving within the same storage kind; a new
        # container's limit metadata must come from that container.
        if p.type != target.kind or p.guid != target.guid:
            p.ClearField('limit_option')
        if target.kind == WAREHOUSE and target.holder.HasField('limit_option'):
            p.limit_option.CopyFrom(target.holder.limit_option)
        p.type, p.guid, p.x, p.y = target.kind, target.guid, x, y

    def can_stack(self, left, right):
        return (left.basic.type_id == right.basic.type_id
                and left.basic.expire_timepoint == right.basic.expire_timepoint
                and left.data.SerializeToString(deterministic=True) == right.data.SerializeToString(deterministic=True))

    def move(self, tx, basic, goal):
        source, item = self.locate(tx, basic)
        count = basic.count or item.basic.count
        if not 1 <= count <= item.basic.count:
            raise ValueError('转移数量无效')
        full = count == item.basic.count
        ignored = (item.basic.guid,) if full else ()
        target, x, y = self.resolve_goal(tx, goal, item, ignored)
        overlaps = self.collisions(target, item, x, y, ignored)
        if overlaps:
            if (target.kind == WORN or len(overlaps) != 1 or overlaps[0].basic.guid == item.basic.guid
                or not self.can_stack(item, overlaps[0])):
                raise ValueError('目标格子已有其他物品')
            stack = overlaps[0]
            maximum = self.dimensions(item.basic.type_id)[2]
            count = min(count, maximum - stack.basic.count)
            if count <= 0:
                raise ValueError('目标物品堆已经满了')
            stack.basic.count += count
            placed = self.copy(stack)
        else:
            placed = self.copy(item)
            placed.basic.count = count
            if not full:
                placed.basic.guid = tx.next_item_guid()
            self.position(placed, target, x, y)
        source.remove(item.basic.guid)
        if count < item.basic.count:
            item.basic.count -= count
            source.append(item)
        if not overlaps:
            target.append(placed)
        return placed, count

    def swap(self, tx, first, second, goal, other_goal=None):
        left, a = self.locate(tx, first)
        right, b = self.locate(tx, second)
        if a.basic.guid == b.basic.guid:
            raise ValueError('无法与同一件物品交换')
        if first.count not in (0,a.basic.count) or second.count not in (0,b.basic.count):
            raise ValueError('交换时必须移动整件物品')
        ignore = (a.basic.guid, b.basic.guid)
        dest_a, ax, ay = self.resolve_goal(tx, goal, a, ignore)
        dest_b, bx, by = self.resolve_goal(tx, other_goal or a.basic.position, b, ignore)
        if self.collisions(dest_a, a, ax, ay, ignore) or self.collisions(dest_b, b, bx, by, ignore):
            raise ValueError('目标格子已有其他物品')
        left.remove(a.basic.guid)
        right.remove(b.basic.guid)
        self.position(a, dest_a, ax, ay)
        dest_a.append(a)
        if self.collisions(dest_b, b, bx, by):
            raise ValueError('交换后物品会重叠')
        self.position(b, dest_b, bx, by)
        dest_b.append(b)
        return a,b

    @staticmethod
    def worn_at(storage, slot):
        return next((item for item in storage.entries() if item.basic.position.x == slot), None)

    def stow_displaced(self, tx, item, goal, source, role_guid):
        if goal and goal.type:
            if goal.type not in (WAREHOUSE, BACKPACK, POUCH, AUTO_WAREHOUSE, AUTO_BACKPACK):
                raise ValueError('卸下的物品必须放回物品栏')
            target, x, y = self.resolve_goal(tx, goal, item)
            try:
                fits = not self.collisions(target, item, x, y)
            except ValueError:
                fits = False
            if fits:
                self.position(item, target, x, y)
                target.append(item)
                return item
            kind, guid = target.kind, target.guid
        else:
            kind = source.kind if source.kind in (WAREHOUSE, BACKPACK, POUCH) else BACKPACK
            guid = source.guid if kind == source.kind else role_guid
        target, x, y = self.auto_target(tx, kind, guid, item)
        self.position(item, target, x, y)
        target.append(item)
        return item

    def wear(self, tx, request, result):
        source, item = self.locate(tx, request.item_basic)
        goal = request.goal_position
        if goal.type != WORN:
            raise ValueError('无效的装备操作')
        if source.kind == WORN and (source.guid != goal.guid or {item.basic.position.x, goal.x} != RING_SLOTS):
            raise ValueError('无效的装备操作')
        target = self.storage(tx, WORN, goal.guid)
        overlaps = self.collisions(target, item, goal.x, goal.y)
        previous = overlaps[0] if overlaps else None
        if source.kind == WORN:
            if previous is None:
                worn, _ = self.move(tx, request.item_basic, goal)
            else:
                destination = request.swap_position if request.swap_position.type else None
                if destination is not None and destination.type == WORN and destination != item.basic.position:
                    raise ValueError('戒指交换目标不是原来的装备栏')
                worn, displaced = self.swap(tx, request.item_basic, previous.basic, goal, destination)
                result.taken_off_item.CopyFrom(displaced)
            result.worn_item.CopyFrom(worn)
            return
        count = request.item_basic.count or item.basic.count
        limit = self.dimensions(item.basic.type_id)[2]
        if not 1 <= count <= min(item.basic.count, limit):
            raise ValueError('装备数量无效')
        paired = self.worn_at(target, WEAPON_PAIRS[goal.x]) if goal.x in WEAPON_PAIRS else None
        secondary = paired if paired is not None and (
            goal.x in RIGHT_WEAPON_SLOTS and self.two_handed(item)
            or goal.x not in RIGHT_WEAPON_SLOTS and self.two_handed(paired)) else None

        if previous is not None and limit > 1 and self.can_stack(item, previous):
            count = min(count, limit - previous.basic.count)
            if count <= 0:
                raise ValueError('该装备栏的物品堆已经满了')
            previous.basic.count += count
            result.worn_item.CopyFrom(previous)
            source.remove(item.basic.guid)
            if count < item.basic.count:
                item.basic.count -= count
                source.append(item)
            return

        worn = self.copy(item)
        worn.basic.count = count
        source.remove(item.basic.guid)
        if count < item.basic.count:
            worn.basic.guid = tx.next_item_guid()
            item.basic.count -= count
            source.append(item)
        self.position(worn, target, goal.x, 0)
        for displaced, destination, response in (
            (previous, request.swap_position, result.taken_off_item),
            (secondary, request.secondary_weapon_position, result.taken_off_secondary_weapon),
        ):
            if displaced is not None:
                copy = self.copy(displaced)
                target.remove(copy.basic.guid)
                response.CopyFrom(self.stow_displaced(tx, copy, destination, source, target.guid))
        target.append(worn)
        result.worn_item.CopyFrom(worn)

    def take_off(self, tx, request, result):
        source, item = self.locate(tx, request.item_basic)
        goal = request.storage_position
        if source.kind != WORN or goal.type not in (WAREHOUSE, BACKPACK, POUCH, AUTO_WAREHOUSE, AUTO_BACKPACK):
            raise ValueError('无效的卸装操作')
        placed, count = self.move(tx, request.item_basic, goal)
        result.taken_off_item.CopyFrom(placed)
        slot = item.basic.position.x
        if count == item.basic.count and slot in RIGHT_WEAPON_SLOTS and request.secondary_weapon_position.type:
            secondary = self.worn_at(source, WEAPON_PAIRS[slot])
            if secondary is not None:
                placed, _ = self.move(tx, secondary.basic, request.secondary_weapon_position)
                result.taken_off_secondary_weapon.CopyFrom(placed)

    def swap_weapon_sets(self, tx, request):
        source, goal = request.ori_position, request.goal_position
        if (source.type != WORN or goal.type != WORN or source.guid != goal.guid
            or {source.x, goal.x} != set(RIGHT_WEAPON_SLOTS) or source.y or goal.y):
            raise ValueError('无效的武器组位置')
        storage = self.storage(tx, WORN, source.guid)
        destinations = {source.x: goal.x, goal.x: source.x,
                        WEAPON_PAIRS[source.x]: WEAPON_PAIRS[goal.x],
                        WEAPON_PAIRS[goal.x]: WEAPON_PAIRS[source.x]}
        for item in storage.entries():
            slot = destinations.get(item.basic.position.x)
            if slot is not None:
                item.basic.position.x = slot

    def store_item(self, tx, item, storage_type=WAREHOUSE, storage_guid=0):
        item = self.copy(item)
        kind = BACKPACK if storage_type in (BACKPACK, AUTO_BACKPACK) else WAREHOUSE
        if item.basic.type_id in self.game_data.extended_stack_ids:
            if kind == WAREHOUSE:
                pages = sorted(tx.profile.user_invetory.inventory_pages, key=lambda p: (p.page_id != storage_guid, p.page_id))
                targets = [Storage(WAREHOUSE, page.page_id, page) for page in pages]
            else:
                targets = [self.storage(tx, BACKPACK, storage_guid or tx.profile.user_hall_setting.role_guid)]
            for target in targets:
                for existing in target.entries():
                    if self.can_stack(item, existing) and existing.basic.count < CONSUMABLE_STACK_LIMIT:
                        amount = min(item.basic.count, CONSUMABLE_STACK_LIMIT - existing.basic.count)
                        existing.basic.count += amount
                        item.basic.count -= amount
                        if item.basic.count == 0:
                            return existing
        target, x, y = self.auto_target(tx, kind, storage_guid, item)
        self.position(item, target, x, y)
        target.append(item)
        return (target.holder.item_group[0].element[-1] if target.kind == WAREHOUSE else target.holder.items[-1])

    def backpack_status(self, tx):
        pages = tx.integer('backpack_pages')
        if not 1 <= pages <= len(BACKPACK_UPGRADE_COSTS) + 1:
            raise ValueError('地牢背包等级无效')
        return dict(pages=pages, row_size=self.bag_page_rows,
                    column_size=int(self.game_data.constants['role_backpack_defualt_column_size']),
                    next_cost=BACKPACK_UPGRADE_COSTS[pages-1] if pages <= len(BACKPACK_UPGRADE_COSTS) else None)

    def upgrade_backpack(self, tx, expected_pages):
        status = self.backpack_status(tx)
        if expected_pages != status['pages']:
            raise ValueError('背包等级已变化，请重新查看')
        if status['next_cost'] is None:
            raise ValueError('地牢背包已经满级')
        if tx.db.execute("SELECT 1 FROM local_runs WHERE status IN ('reserved','active') LIMIT 1").fetchone():
            raise ValueError('请回到营地后再升级地牢背包')
        coin = tx.coin()
        if coin.count < status['next_cost']:
            raise ValueError('金币不足')
        coin.count -= status['next_cost']
        pages = status['pages'] + 1
        tx.set_integer('backpack_pages', pages)
        for role in tx.profile.user_role:
            role.deploy_role.backpack.row_size = self.bag_page_rows * pages
        return self.backpack_status(tx)

    def store_new_item(self, transaction, type_id, count, storage_type=1, storage_guid=0, template=None):
        if not 1 <= count <= 100000:
            raise ValueError('invalid item quantity')
        _, _, stack = self.dimensions(type_id)
        created = []
        while count:
            amount = min(count, stack)
            if len(created) >= 1024:
                raise ValueError('one purchase creates too many inventory instances')
            if storage_type not in (0,WAREHOUSE,BACKPACK,AUTO_WAREHOUSE,AUTO_BACKPACK):
                raise ValueError('unsupported purchase destination')
            item = self.protocol.message('tgf.DItem')
            if template is not None:
                item.CopyFrom(template)
                item.ClearField('basic')
            item.basic.type_id = type_id
            item.basic.guid = transaction.next_item_guid()
            item.basic.count = amount
            created.append(self.store_item(transaction, item, storage_type, storage_guid))
            count -= amount
        return created
