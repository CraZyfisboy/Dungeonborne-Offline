-- The original loose-item effect component uses the donor blueprint's quality.
-- Use the same Niagara assets with the quality of the actual native item ID.
local M={}
local loot=require("OfflineProbe.LootVisuals")
local collectable_path="/Game/TGF/Environment/Collectable/BP_CollectableBase.BP_CollectableBase_C"
local chest_path="/Game/TGF/Environment/Chests/BP_InteractableChestBase.BP_InteractableChestBase_C"
local systems={[6]="FX_Common_Drop_Orange",[7]="FX_Common_Drop_Golden"}
local original_systems={
    FX_Common_Drop_Green_01=true, FX_Common_Drop_Blue_01=true, FX_Common_Drop_Purple_01=true,
    FX_Common_Drop_Orange=true, FX_Common_Drop_Golden=true, FX_Common_Drop_Pink_01=true,
}
local assets={}
local collectable_class,chest_class,next_check,last_error

function M.refresh_collectable(actor)
    local item=actor["Inventory Instance"]
    local type_id=item and item.type_id
    local desired=type_id and type_id>0 and systems[loot.rarity(type_id)]
    if not desired then return end
    local components=actor:K2_GetComponentsByClass(UE.UNiagaraComponent.StaticClass())
    for i=1,components:Num() do
        local component=components:Get(i)
        local asset=component.Asset
        local current=asset and UE.UKismetSystemLibrary.GetObjectName(asset)
        if original_systems[current] then
            if current~=desired then
                assets[desired]=assets[desired] or LoadObject("/Game/TGF/VFX/Niagaras/" .. desired .. "." .. desired)
                if assets[desired] then component:SetAsset(assets[desired],true); component:Activate(true) end
            end
            local actual=component.Asset
            return actual and UE.UKismetSystemLibrary.GetObjectName(actual)
        end
    end
end

function M.tick(gi,ready,mark)
    if not ready then next_check,last_error=nil,nil; return end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if next_check and now<next_check and next_check-now<=0.5 then return end
    next_check=now+0.5
    local ok,err=pcall(function()
        collectable_class=collectable_class or UE.UClass.Load(collectable_path)
        chest_class=chest_class or UE.UClass.Load(chest_path)
        local actors=UE.TArray(UE.AActor)
        if collectable_class then
            UE.UGameplayStatics.GetAllActorsOfClass(gi,collectable_class,actors)
            for i=1,actors:Num() do M.refresh_collectable(actors:Get(i)) end
        end
        if chest_class then
            UE.UGameplayStatics.GetAllActorsOfClass(gi,chest_class,actors)
            for i=1,actors:Num() do
                local chest=actors:Get(i)
                if chest.Looted then
                    local inventory=chest:GetComponentByClass(UE.UTgfInventoryManagerComponent)
                    if inventory then loot.refresh_container(chest,inventory) end
                end
            end
        end
    end)
    if not ok and err~=last_error then mark("original equipment beam: " .. tostring(err)) end
    last_error=err
end

return M
