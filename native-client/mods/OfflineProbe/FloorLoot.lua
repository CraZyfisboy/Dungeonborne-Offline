-- Native Blueprint loot calls use LootType 0 on both floors. Roll the
-- additional level-two table into the same original inventory once.
local M={}
local second_floors={
    L_Dungeon_Official_02=true,
    L_Dungeon_Official_02_Hard=true,
    L_GothicCastle_WP_Main_SecondFloor=true,
    L_GothicCastle_WP_Main_SecondFloor_Hard=true,
}
local active_level,active_map,configured,sources
local awarded={}

function M.enter(level,map_id)
    if active_level==level and active_map==map_id then return end
    active_level,active_map=level,map_id
    awarded,sources={},nil
    if second_floors[level] then
        configured=configured or require("OfflineProbe.FloorLootRules")
        sources=configured.maps[map_id]
    end
end

function M.apply(container,inventory)
    if not sources or not container:HasAuthority() then return 0 end
    local key=UE.UKismetSystemLibrary.GetObjectName(container)
    if awarded[key] then return 0 end
    local class=UE.UKismetSystemLibrary.GetClassDisplayName(container:GetClass())
    if not sources[class] then return 0 end
    local gi=UE.UTGFBaseFunctionLibrary.GetGameInstance()
    local controller=UE.UGameplayStatics.GetPlayerController(gi,0)
    local state=controller and controller.PlayerState
    if not state then return 0 end
    local players=UE.TArray(UE.APlayerState)
    players:Add(state)
    local name=UE.UKismetSystemLibrary.GetDisplayName(container)
    local items=UE.UBattleUtility.Loot(container,name,"",players,state,class,configured.loot_type)
    awarded[key]=true
    local count=0
    for i=1,items:Num() do
        local item=items:Get(i)
        if inventory:CanAddItemInstance(item,0) then
            inventory:AddItemInstanceAuth(item,0)
            count=count+1
        end
    end
    return count
end

return M
