-- The map-authored gold chest uses a kill counter inherited from Guarded.
local M={}
local class_path="/TgfMaps/SceneComp/Blueprint_Dynamic/TreasureChest/" ..
    "BP_TreasureChest_Gold_01_Special.BP_TreasureChest_Gold_01_Special_C"
local class,next_check

function M.tick(gi,ready)
    if not ready then
        next_check=nil
        return
    end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if next_check and now<next_check then return end
    next_check=now+1
    class=class or UE.UClass.Load(class_path)
    if not class then return end
    local chests=UE.TArray(UE.AActor)
    UE.UGameplayStatics.GetAllActorsOfClass(gi,class,chests)
    for i=1,chests:Num() do
        local chest=chests:Get(i)
        if chest:HasAuthority() and chest.bLocked then
            chest.MonsterNumToKill=0
            chest:FlushNetDormancy()
            chest.bLocked=false
            chest:ForceNetUpdate()
        end
    end
end

return M
