-- These two encounters use authored points, one of which streams in with the rooftop.
local M={}
local spawn_class_path="/Game/TGF/Environment/ActorSpawn/BP_ActorSpawnPoint.BP_ActorSpawnPoint_C"
local monster_path="/Game/TGF/Character/Monster/"
local cyclops={
    L_GothicCastle_WP_Main_Classic="BP_TGF_Monster_Cyclop",
    L_GothicCastle_WP_Main_Hard="BP_TGF_H_Monster_Cyclop",
}
local wolf_name="BP_TGF_H_Werewolf"
local roof_point="04421A97AF93C41D02_1376977239"
local wolf_point="2C58B9926278F51B02_1165986748"
local spawn_class,playing,run
local boss_classes={}

local function boss_class(folder,name)
    boss_classes[name]=boss_classes[name] or UE.UClass.Load(monster_path .. folder .. "/" .. name .. "." .. name .. "_C")
    return boss_classes[name]
end

local function roof_floor(gi,point,pawn)
    local origin=point:K2_GetActorLocation()
    local ignored=UE.TArray(UE.AActor)
    ignored:Add(pawn)
    local hit=UE.FHitResult()
    local floor=UE.UKismetSystemLibrary.LineTraceSingle(gi,
        origin+UE.FVector(0,0,120),origin-UE.FVector(0,0,180),
        0,false,ignored,0,hit,true,UE.FLinearColor(),UE.FLinearColor(),0)
    if floor and hit.ImpactNormal.Z>0.6 then return hit.ImpactPoint end
end

local function keep_cyclops_on_roof(point,floor,mark)
    local actors=point.SpawnedActors
    for i=1,actors:Num() do
        local actor=actors:Get(i)
        if actor and actor:IsA(point.DataActor) and not actor:IsDeadOrDying() then
            local origin=point:K2_GetActorLocation()
            local pos=actor:K2_GetActorLocation()
            local height=floor.Z+actor.CapsuleComponent:GetScaledCapsuleHalfHeight()
            if pos.Z>=height-150 then return true end
            local offset=pos-origin
            if offset.X^2+offset.Y^2>650^2 then return true end
            -- The native room may spawn before its collision cell is ready.
            -- Recover that same living actor at its own point once the floor exists.
            actor:GetMovementComponent():StopMovementImmediately()
            if not actor:K2_SetActorLocation(UE.FVector(origin.X,origin.Y,height+5),false,UE.FHitResult(),true) then
                return false
            end
            mark(string.format("returned original Cyclops from streamed lower floor %.1f to its roof %.1f",pos.Z,height+5))
            return true
        end
    end
    return true
end

local function player_entered_spawn_room(point,pawn)
    local set=point:GetAttachParentActor()
    local room=set and set:GetAttachParentActor()
    return room and room.PlayerDistanceChecker and room.DistanceThreshold>0
        and room:GetDistanceTo(pawn)<=room.DistanceThreshold
end

local function wolf_floor(gi,position,pawn,actor,up,down)
    local ignored=UE.TArray(UE.AActor)
    ignored:Add(pawn)
    if actor then ignored:Add(actor) end
    local hit=UE.FHitResult()
    local found=UE.UKismetSystemLibrary.LineTraceSingle(gi,
        position+UE.FVector(0,0,up or 160),position-UE.FVector(0,0,down or 180),
        0,false,ignored,0,hit,true,UE.FLinearColor(),UE.FLinearColor(),0)
    if found and hit.ImpactNormal.Z>0.6 then return hit.ImpactPoint end
end

local function settle_wolf(gi,point,pawn,mark)
    local actors=point.SpawnedActors
    if actors:Num()==0 then return end
    local actor=actors:Get(1)
    if not actor or not actor:IsA(point.DataActor) or actor:IsDeadOrDying() then return true end
    local origin=point:K2_GetActorLocation()
    local source_floor=wolf_floor(gi,origin,pawn,actor)
    if not source_floor then return false end
    local position=actor:K2_GetActorLocation()
    local capsule=actor.CapsuleComponent
    local half_height=capsule:GetScaledCapsuleHalfHeight()
    local destination
    -- Older fallback code could use the uninitialized transform cache at world zero.
    if origin.X^2+origin.Y^2>2000^2 and position.X^2+position.Y^2<512^2 then
        destination=UE.FVector(origin.X,origin.Y,source_floor.Z)
    else
        local delta=position-origin
        local floor=wolf_floor(gi,position,pawn,actor)
        if floor and position.Z-half_height<floor.Z-25 then
            destination=UE.FVector(position.X,position.Y,floor.Z)
        elseif delta.X^2+delta.Y^2<=1200^2 and position.Z+half_height<source_floor.Z then
            -- The local downward trace can hit the lower storey after the
            -- wolf has already fallen through the encounter's loaded floor.
            local above=UE.FVector(position.X,position.Y,source_floor.Z)
            local room_floor=wolf_floor(gi,above,pawn,actor,90,90)
            if room_floor and room_floor.Z>position.Z+half_height then
                destination=UE.FVector(origin.X,origin.Y,source_floor.Z)
            elseif not floor then
                destination=UE.FVector(origin.X,origin.Y,source_floor.Z)
            end
        elseif not floor and position.Z+half_height<source_floor.Z-250 then
            -- A still-falling wolf can drift outside its original patrol
            -- radius while the surrounding cells stream.
            destination=UE.FVector(origin.X,origin.Y,source_floor.Z)
        end
    end
    if not destination then return true end
    destination.Z=destination.Z+half_height+5
    actor:GetMovementComponent():StopMovementImmediately()
    if not actor:K2_SetActorLocation(destination,false,UE.FHitResult(),true) then return false end
    mark(string.format("restored original Werewolf to loaded authored ground %.1f %.1f %.1f",
        destination.X,destination.Y,destination.Z))
    return false
end

local function check_wolf(gi,point,pawn,mark)
    if not player_entered_spawn_room(point,pawn) then return end
    if not point:HasAnySpawnedActor() then
        local options=point.ActorSpawnOptionComponent
        if run.wolf_spawned or (options and not options:CanSpawn())
            or not wolf_floor(gi,point:K2_GetActorLocation(),pawn) then return end
        point:Spawn()
        point:SpawnImmediately()
        if not point:HasAnySpawnedActor() then return end
        mark("local Werewolf initialized by its original castle spawner")
    end
    run.wolf_spawned=true
    settle_wolf(gi,point,pawn,mark)
end

function M.arm()
    run={encounters={}}
end

function M.tick(gi,level,ready,mark)
    local cyclops_name=cyclops[level]
    if not run or not ready or not cyclops_name then return end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if run.next_check and now<run.next_check then return end
    run.next_check=now+1
    local phase=UE.USubsystemBlueprintLibrary.GetWorldSubsystem(gi,UE.ULyraGamePhaseSubsystem)
    playing=playing or UE.UTgfGameWidgetLibrary.GetGameplayTagFromString("TGF.GamePhase.Playing")
    if not phase or not phase:IsPhaseActive(playing) then return end
    local mode=UE.UGameplayStatics.GetGameMode(gi)
    local pawn=UE.UGameplayStatics.GetPlayerPawn(gi,0)
    if not mode or not mode.bCanSpawnActor or not pawn or not pawn:IsA(UE.ALyraCharacter) then return end
    spawn_class=spawn_class or UE.UClass.Load(spawn_class_path)
    local roof_class=boss_class("M_Cyclop",cyclops_name)
    local wolf_class=boss_class("M_Werewolf",wolf_name)
    if not spawn_class or not roof_class or not wolf_class then return end
    if run.wolf_point and not UE.UKismetSystemLibrary.IsValid(run.wolf_point) then run.wolf_point=nil end
    if run.wolf_point then
        local ok,err=pcall(check_wolf,gi,run.wolf_point,pawn,mark)
        if not ok and run.wolf_error~=err then
            run.wolf_error=err
            mark("original Werewolf spawner: " .. tostring(err))
        end
        if run.encounters.Cyclops then return end
    end
    if run.next_scan and now<run.next_scan then return end
    run.next_scan=now+3
    local actors=UE.TArray(UE.AActor)
    UE.UGameplayStatics.GetAllActorsOfClass(gi,spawn_class,actors)
    for i=1,actors:Num() do
        local point=actors:Get(i)
        local key
        if point and point:HasAuthority() then
            if not run.encounters.Cyclops and point.DataActor==roof_class and point.DataMonsterCombatZone
                and UE.UKismetSystemLibrary.GetObjectName(point):find(roof_point,1,true) then
                key="Cyclops"
            elseif not run.wolf_point and point.DataActor==wolf_class and point.DeathTriggerMechanisms:Num()>0
                and UE.UKismetSystemLibrary.GetObjectName(point):find(wolf_point,1,true) then
                run.wolf_point=point
            end
        end
        if key then
            local ok,spawned=pcall(function()
                local occupied=point:HasAnySpawnedActor()
                if not player_entered_spawn_room(point,pawn) then return false end
                local options=point.ActorSpawnOptionComponent
                if options and not options:CanSpawn() then return false end
                if key=="Cyclops" then
                    local floor=roof_floor(gi,point,pawn)
                    if not floor then
                        if not run.waiting_for_roof then
                            run.waiting_for_roof=true
                            mark("waiting for collision at the original Cyclops rooftop")
                        end
                        return false
                    end
                    if occupied then
                        if not run.waiting_for_roof then return true end
                        return keep_cyclops_on_roof(point,floor,mark)
                    end
                end
                -- Spawn initializes the transient transform/class cache and native messages.
                -- SpawnImmediately retains the original duplicate guard and monster setup.
                point:Spawn()
                point:SpawnImmediately()
                if not point:HasAnySpawnedActor() then return false end
                mark("local " .. key .. " initialized by its original castle spawner")
                return true
            end)
            if ok and spawned then
                run.encounters[key]=true
            elseif not ok and run.error~=spawned then
                run.error=spawned
                mark("original " .. key .. " spawner: " .. tostring(spawned))
            end
        end
    end
end

return M
