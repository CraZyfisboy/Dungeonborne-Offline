-- Local Crypt Hard can leave authored fixed escape points without a device.
-- Spawn the original class through the placed point, retaining its gate,
-- effects, position, activation delay and use limits.
local M={}
local first_floors={
    L_GothicCastle_WP_Main=true,
    L_GothicCastle_WP_Main_Classic=true,
    L_GothicCastle_WP_Main_Hard=true,
    L_Dungeon_Official_01=true,
    L_Dungeon_Official_01_Classic=true,
    L_Dungeon_Official_01_Hard=true,
}
local floor,world_mode
local restored={}
local placed={}
local next_fixed_check,next_blue_check=0,0
local point_class,device_class,extract_class,playing_tag
local death_zone
local scheduled=false

local function playing_world(gi)
    local pawn=UE.UGameplayStatics.GetPlayerPawn(gi,0)
    if not pawn or not pawn:IsA(UE.ALyraCharacter) or not pawn:HasAuthority() then return end
    local mode=UE.UGameplayStatics.GetGameMode(gi)
    if not mode or not mode.bCanSpawnActor then return end
    local phases=UE.USubsystemBlueprintLibrary.GetWorldSubsystem(pawn,UE.ULyraGamePhaseSubsystem)
    playing_tag=playing_tag or UE.UTgfGameWidgetLibrary.GetGameplayTagFromString("TGF.GamePhase.Playing")
    if not phases or not phases:IsPhaseActive(playing_tag) then return end
    return mode
end

local function restore_fixed(gi,mode,mark)
    point_class=point_class or assert(UE.UClass.Load("/Game/TGF/Environment/Escapes/" ..
        "BP_EscapeDeviceSpawnPoint.BP_EscapeDeviceSpawnPoint_C"))
    device_class=device_class or assert(UE.UClass.Load("/Game/TGF/Environment/Escapes/" ..
        "BP_EscapeDeviceBase.BP_EscapeDeviceBase_C"))
    local points=UE.TArray(UE.AActor)
    UE.UGameplayStatics.GetAllActorsOfClass(gi,point_class,points)
    local count=0
    for i=1,points:Num() do
        local point=points:Get(i)
        local name=UE.UKismetSystemLibrary.GetPathName(point)
        if not restored[name] then
            if point:HasAnySpawnedActor() then
                restored[name]=true
            else
                local options=point.ActorSpawnOptionComponent
                local actor=point.DataActor
                if point.Chance==1 and options and options:CanSpawn() and actor
                    and UE.UKismetMathLibrary.ClassIsChildOf(actor,device_class) then
                    -- GetTransformToSpawn reads a cache populated by the native
                    -- Spawn event, which this local fallback has not executed.
                    if point:SpawnActor(mode,actor,point:GetTransform()) then
                        restored[name]=true
                        count=count+1
                    end
                end
            end
        end
    end
    if count>0 then mark("restored original authored Crypt Hard fixed exits: " .. count) end
end

local function horizontal_distance_squared(a,b)
    local x,y=a.X-b.X,a.Y-b.Y
    return x*x+y*y
end

local function within_wave(point,current_center,current_radius,target)
    return horizontal_distance_squared(point,current_center)<=current_radius*current_radius
        and horizontal_distance_squared(point,target.Center)<=target.Radius*target.Radius
end

local function is_unoccupied(point,locations)
    for _,other in ipairs(locations) do
        local z=point.Z-other.Z
        if horizontal_distance_squared(point,other)+z*z<40000 then return false end
    end
    return true
end

local function choose_authored_point(gi,candidates)
    local first=UE.UKismetMathLibrary.RandomIntegerInRange(1,#candidates)
    local ok,near_nav=pcall(function()
        local projected,extent=UE.FVector(),UE.FVector(120,120,200)
        for offset=0,#candidates-1 do
            local point=candidates[(first+offset-1)%#candidates+1]
            if UE.UNavigationSystemV1.K2_ProjectPointToNavigation(gi,point,projected,nil,nil,extent) then
                return point
            end
        end
    end)
    if ok and near_nav then return near_nav,true end
    -- Local streaming can leave valid authored rooms without a loaded nav mesh.
    return candidates[first],false
end

-- Extend the native pending wave for solo access. The shipped final wave
-- chooses purple only 25% of the time and contains no dynamic red entry.
-- The original death-zone timer creates the devices and emits the map notices.
local function schedule_progression(gi,mark)
    if scheduled then return end
    death_zone=death_zone or UE.UGameplayStatics.GetActorOfClass(gi,UE.ADeathZone)
    if not death_zone then return end
    local discs=death_zone["Precomputed Discs"]
    if not discs or discs:Num()==0 then return end
    local final_index
    for i=1,discs:Num() do
        local disc=discs:Get(i)
        if disc.Radius>1 then
            for j=1,disc.PortalSpawnInfos:Num() do
                local info=disc.PortalSpawnInfos:Get(j)
                if info.PortalClass and info.SpawnProbability>0 then
                    final_index=i
                end
            end
        end
    end
    if not final_index then return end
    local final=discs:Get(final_index)
    -- SpawnTime is relative to each disc, so take it only from the final one.
    local spawn_time=0
    local occupied,has_red,has_shadow={},false,false
    local kinds=UE.ETgfPortalType
    for i=1,final.PortalSpawnInfos:Num() do
        local info=final.PortalSpawnInfos:Get(i)
        if info.PortalClass and info.SpawnProbability>0 then
            spawn_time=math.max(spawn_time,info.SpawnTime)
            occupied[#occupied+1]=info.SpawnTransform.Translation
            local definition=UE.UDataUtility.GetClassDefaultObject(info.PortalClass)
            local kind=definition:GetClassPortalType()
            has_red=has_red or (kind==kinds.RedDevice and info.SpawnProbability>=1)
            has_shadow=has_shadow or (kind==kinds.ShadowDevice and info.SpawnProbability>=1)
        end
    end
    local classes={}
    if not has_red then classes[#classes+1]="BP_ExtractDevice_RedOne" end
    if not has_shadow then classes[#classes+1]="BP_ExtractDevice_ShadowOne" end
    local additions={}
    for _,name in ipairs(classes) do
        local candidates={}
        for i=1,death_zone.PotentialPortalPlacements:Num() do
            local point=death_zone.PotentialPortalPlacements:Get(i)
            if horizontal_distance_squared(point,final.Center)<=final.Radius*final.Radius
                and is_unoccupied(point,occupied) then candidates[#candidates+1]=point end
        end
        if #candidates==0 then return end
        local point=choose_authored_point(gi,candidates)
        occupied[#occupied+1]=point
        local info=UE.FTgfPortalSpawnInfo()
        info.SpawnTime=spawn_time
        info.SpawnProbability=1
        info.SpawnTransform=UE.FTransform(UE.FRotator(),point,UE.FVector(1,1,1))
        info.PortalClass=assert(UE.UClass.Load("/Game/TGF/Environment/Escapes/" .. name .. "." .. name .. "_C"))
        additions[#additions+1]=info
    end
    local before=final.PortalSpawnInfos:Num()
    local queue={}
    for i=1,before do queue[#queue+1]={info=final.PortalSpawnInfos:Get(i),order=i} end
    for _,info in ipairs(additions) do queue[#queue+1]={info=info,order=#queue+1} end
    table.sort(queue,function(a,b)
        if a.info.SpawnTime==b.info.SpawnTime then return a.order<b.order end
        return a.info.SpawnTime<b.info.SpawnTime
    end)
    local pending=UE.TArray(UE.FTgfPortalSpawnInfo)
    for _,entry in ipairs(queue) do pending:Add(entry.info) end
    final.PortalSpawnInfos=pending
    discs:Set(final_index,final)
    assert(discs:Get(final_index).PortalSpawnInfos:Num()==before+#additions,"native portal schedule write did not persist")
    scheduled=true
    mark(string.format("solo progression added %d native portal schedule entries: %s disc=%d time=%.1f",
        #additions,floor,final_index-1,spawn_time))
end

local function place_dynamic_devices(gi,mark)
    extract_class=extract_class or assert(UE.UClass.Load("/Game/TGF/Environment/Escapes/" ..
        "BP_ExtractDevice_Base.BP_ExtractDevice_Base_C"))
    local devices=UE.TArray(UE.AActor)
    UE.UGameplayStatics.GetAllActorsOfClass(gi,extract_class,devices)
    local kinds=UE.ETgfPortalType
    local pending,locations={},{}
    for i=1,devices:Num() do
        local device=devices:Get(i)
        locations[i]=device:K2_GetActorLocation()
        local kind=device:GetClassPortalType()
        if kind==kinds.BlueDevice or kind==kinds.RedDevice or kind==kinds.ShadowDevice then
            local name=UE.UKismetSystemLibrary.GetPathName(device)
            if not placed[name] then pending[#pending+1]={device=device,name=name,index=i} end
        end
    end
    if #pending==0 then return end
    death_zone=death_zone or UE.UGameplayStatics.GetActorOfClass(gi,UE.ADeathZone)
    if not death_zone then return end
    local discs=death_zone["Precomputed Discs"]
    local disc_index=death_zone["Current Disc Index"]
    if not discs or disc_index<0 or disc_index>=discs:Num() then return end
    -- The original Blueprint spawns this wave against this precomputed disc.
    -- Its Next Center/Radius describe the following wave, including closure.
    local target=discs:Get(disc_index+1)
    local current_center,current_radius=death_zone["Dynamic Center"],death_zone["Dynamic Radius"]
    if current_radius<=0 or target.Radius<=0 then return end
    local authored=death_zone.PotentialPortalPlacements
    if not authored or authored:Num()==0 then return end
    for _,entry in ipairs(pending) do
        placed[entry.name]=true
        local old=locations[entry.index]
        if not within_wave(old,current_center,current_radius,target) then
            local candidates={}
            for i=1,authored:Num() do
                local point=authored:Get(i)
                if within_wave(point,current_center,current_radius,target) and is_unoccupied(point,locations) then
                    candidates[#candidates+1]=point
                end
            end
            if #candidates==0 then
                mark("original dynamic portal kept its location: no authored point inside disc " .. disc_index)
            else
                local point,near_nav=choose_authored_point(gi,candidates)
                if entry.device:K2_SetActorLocation(point,false,UE.FHitResult(),true) then
                    locations[entry.index]=point
                    mark(string.format("original dynamic portal placed inside disc %d: %.0f,%.0f,%.0f; nearby navigation=%s",
                        disc_index,point.X,point.Y,point.Z,tostring(near_nav)))
                else
                    mark("original dynamic portal kept its location: authored point move failed in disc " .. disc_index)
                end
            end
        end
    end
end

function M.tick(gi,level,ready,mark)
    if not first_floors[level] then
        if floor then floor,world_mode,restored,placed,death_zone,scheduled=nil,nil,{},{},nil,false end
        return
    end
    if not ready then return end
    local hard=level=="L_Dungeon_Official_01_Hard"
    -- Waiting-room advancement can reload the same map name in a new world.
    -- Never retain its old death-zone queue or placed-device records.
    local mode_identity=UE.UGameplayStatics.GetGameMode(gi)
    if floor~=level or world_mode~=mode_identity then
        floor,world_mode,restored,placed,death_zone,scheduled=level,mode_identity,{},{},nil,false
        next_fixed_check,next_blue_check=0,0
    end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if (not hard or now<next_fixed_check) and now<next_blue_check then return end
    local world_ok,mode=pcall(playing_world,gi)
    if not world_ok then
        mark("original Crypt portal phase: " .. tostring(mode))
        next_fixed_check,next_blue_check=now+30,now+30
        return
    end
    if not mode then
        next_fixed_check,next_blue_check=now+0.5,now+0.5
        return
    end
    if hard and now>=next_fixed_check then
        next_fixed_check=now+2
        local ok,fault=pcall(restore_fixed,gi,mode,mark)
        if not ok then
            mark("original fixed exits: " .. tostring(fault))
            next_fixed_check=now+30
        end
    end
    if now>=next_blue_check then
        next_blue_check=now+0.5
        local ok,fault=pcall(function()
            schedule_progression(gi,mark)
            place_dynamic_devices(gi,mark)
        end)
        if not ok then
            mark("original dynamic portals: " .. tostring(fault))
            next_blue_check=now+30
        end
    end
end

return M
