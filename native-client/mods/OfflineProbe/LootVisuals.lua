-- The original monster graph ranks asset fragments when it creates loot.
-- Recheck the remaining native inventory against the effective item tables.
local M={}
local floor_loot=require("OfflineProbe.FloorLoot")
local monster_path="/Game/TGF/Character/Monster/BP_Tgf_Monster.BP_Tgf_Monster_C"
local monster_class,next_check,last_error
local item_types,source_types
-- Exceptional retains its original pink asset, below the two top equipment tiers.
local priorities={[0]=0,[1]=1,[2]=2,[3]=3,[4]=4,[5]=5,[8]=6,[6]=7,[7]=8}

function M.rarity(type_id)
    if not item_types or not source_types then
        local tables=require("Table.TGFTableManager")
        item_types=tables:GetTabbleByName("ExcelItemType")
        source_types=tables:GetTabbleByName("UESourceItemType")
    end
    if not item_types or not source_types then return end
    local item=item_types:GetByIndex("type_id",type_id)
    local source_id=item and item.ue_source_type_id or type_id
    if source_id==0 then source_id=type_id end
    local source=source_types:GetByIndex("type_id",source_id)
    return source and source.rarity
end

function M.refresh_container(container,inventory)
    floor_loot.apply(container,inventory)
    local items=inventory:GetAllItems()
    local ignored={}
    local exclusions=container.ItemTypesToDiscardBeam
    if exclusions then
        for i=1,exclusions:Num() do ignored[exclusions:Get(i)]=true end
    end
    local highest=0
    for i=1,items:Num() do
        local item=items:Get(i)
        local class=item:GetStackCount()>0 and item:GetItemDef(false)
        local definition=class and UE.UDataUtility.GetClassDefaultObject(class)
        if definition then
            local rarity=M.rarity(item.type_id)
            if not priorities[rarity] then
                local icon=item:FindFragmentByClass(UE.UInventoryFragment_InventoryIcon.StaticClass(),false)
                rarity=icon and icon.DisplayQuality
            end
            local priority=priorities[rarity] or 0
            local visible=priority>=priorities[5] or (not definition.IgnoreLootBeam and not ignored[definition.ItemType])
            if visible and priority>priorities[highest] then highest=rarity end
        end
    end
    if container.MaxQuality~=highest then
        container.MaxQuality=highest
        container:OnRep_MaxQuality()
    end
    return highest
end

function M.refresh(monster)
    local actor_type=monster:GetActorType()
    if actor_type~=11 and actor_type~=12 and actor_type~=13 then return end
    local attributes=monster.AttributeComponent
    if not monster.Looted or not attributes or not attributes:GetDeadOrDying() then return end
    local inventory=monster:GetComponentByClass(UE.UTgfInventoryManagerComponent)
    if inventory then return M.refresh_container(monster,inventory) end
end

function M.tick(gi,ready,mark)
    if not ready then next_check,last_error=nil,nil; return end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if next_check and now<next_check and next_check-now<=0.5 then return end
    next_check=now+0.5
    local ok,err=pcall(function()
        monster_class=monster_class or UE.UClass.Load(monster_path)
        if not monster_class then return end
        local monsters=UE.TArray(UE.AActor)
        UE.UGameplayStatics.GetAllActorsOfClass(gi,monster_class,monsters)
        for i=1,monsters:Num() do M.refresh(monsters:Get(i)) end
    end)
    if not ok and err~=last_error then mark("original corpse beam: " .. tostring(err)) end
    last_error=err
end

return M
