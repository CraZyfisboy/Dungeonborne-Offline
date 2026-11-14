-- Native red/shadow gates already travel with the live character. Local world
-- initialization resets the scene tag, so reapply it for the native shadow start.
local M={}
local destinations={
    L_Dungeon_Official_01="L_Dungeon_Official_02",
    L_Dungeon_Official_01_Classic="L_Dungeon_Official_02",
    L_Dungeon_Official_01_Hard="L_Dungeon_Official_02_Hard",
    L_GothicCastle_WP_Main="L_GothicCastle_WP_Main_SecondFloor",
    L_GothicCastle_WP_Main_Classic="L_GothicCastle_WP_Main_SecondFloor",
    L_GothicCastle_WP_Main_Hard="L_GothicCastle_WP_Main_SecondFloor_Hard",
}
local shadow_tag,transition,last_error
local shadow_scene=1

function M.arm()
    transition,last_error=nil,nil
end

local function shadow_starts(gi)
    local actors=UE.TArray(UE.AActor)
    UE.UGameplayStatics.GetAllActorsOfClass(gi,UE.ALyraPlayerStart,actors)
    local starts={}
    for i=1,actors:Num() do
        local start=actors:Get(i)
        if start.PlayerSceneTag==shadow_scene and not start:IsWaitingPoint() and not start:IsDeathPoint() then
            starts[#starts+1]=start
        end
    end
    return starts
end

local function by_shadow_start(pawn,starts)
    local pos=pawn:K2_GetActorLocation()
    for _,start in ipairs(starts) do
        local offset=pos-start:K2_GetActorLocation()
        if offset.X*offset.X+offset.Y*offset.Y<640000 and math.abs(offset.Z)<450 then return true end
    end
    return false
end

local function floor_ready(gi,pawn,position)
    local ignored=UE.TArray(UE.AActor)
    ignored:Add(pawn)
    local hit=UE.FHitResult()
    return UE.UKismetSystemLibrary.LineTraceSingle(gi,position+UE.FVector(0,0,100),
        position-UE.FVector(0,0,330),0,false,ignored,0,hit,true,
        UE.FLinearColor(),UE.FLinearColor(),0) and hit.ImpactNormal.Z>0.65
end

local function restore(gi,level,pawn,state,mark)
    if not transition or transition.destination~=level or not state then return end
    if state:GetPlayerSceneTag()~=shadow_scene then state:SetPlayerSceneTag(shadow_scene) end
    if not pawn or not pawn:IsA(UE.ALyraCharacter) or not pawn:HasAuthority() then return end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if transition.next_check and now<transition.next_check then return end
    transition.next_check=now+0.2
    transition.wait_until=transition.wait_until or now+15
    local movement=pawn:GetMovementComponent()
    if not movement then return end
    if not transition.arrival then
        local starts=shadow_starts(gi)
        local flow=state:GetComponentByClass(UE.UTgfPlayerGameFlow)
        if #starts==0 or not flow then
            if now>=transition.wait_until then
                state:SetPlayerSceneTag(0)
                UE.UTgfUIBlueprintLibrary.ShowToast("暗影入口加载失败，已保留当前楼层的入口")
                mark("original Shadow start was unavailable after the world loaded")
                transition=nil
            end
            return
        end
        local before=pawn:K2_GetActorLocation()
        local rotation=pawn:K2_GetActorRotation()
        movement:DisableMovement()
        if not by_shadow_start(pawn,starts) then flow:TeleportToChosenPoint(false) end
        local original_choice=by_shadow_start(pawn,starts)
        if not original_choice then
            local start=starts[UE.UKismetMathLibrary.RandomIntegerInRange(1,#starts)]
            pawn:K2_SetActorLocationAndRotation(start:K2_GetActorLocation()+UE.FVector(0,0,100),
                start:K2_GetActorRotation(),false,UE.FHitResult(),true)
        end
        transition.arrival={position=pawn:K2_GetActorLocation(),before=before,rotation=rotation,
            original_choice=original_choice,deadline=now+15}
    end
    local arrival=transition.arrival
    if floor_ready(gi,pawn,arrival.position) then
        movement:SetMovementMode(1,0)
        mark("original Shadow start ready on " .. level .. "; native choice=" .. tostring(arrival.original_choice))
        transition=nil
    elseif now>=arrival.deadline then
        pawn:K2_SetActorLocationAndRotation(arrival.before,arrival.rotation,false,UE.FHitResult(),true)
        movement:SetMovementMode(1,0)
        state:SetPlayerSceneTag(0)
        UE.UTgfUIBlueprintLibrary.ShowToast("暗影区域加载失败，已回到当前楼层的入口")
        mark("original Shadow start did not finish collision streaming")
        transition=nil
    else
        movement:DisableMovement()
    end
end

function M.tick(gi,level,ready,mark)
    if not ready then return end
    local pc=UE.UGameplayStatics.GetPlayerController(gi,0)
    local state=pc and pc.PlayerState
    local pawn=UE.UGameplayStatics.GetPlayerPawn(gi,0)
    local destination=destinations[level]
    if destination and not transition and pawn and state and state:GetPlayerSceneTag()==shadow_scene then
        shadow_tag=shadow_tag or UE.UTgfGameWidgetLibrary.GetGameplayTagFromString("TGF.ActorLevelState.InPortal.Shadow")
        if UE.UAbilitySystemBlueprintLibrary.HasMatchingGameplayTag(pawn,shadow_tag) then
            transition={destination=destination}
        end
    elseif transition and transition.destination==level then
        local ok,err=pcall(restore,gi,level,pawn,state,mark)
        if not ok and err~=last_error then mark("original Shadow arrival: " .. tostring(err)) end
        last_error=err
    end
end

return M
