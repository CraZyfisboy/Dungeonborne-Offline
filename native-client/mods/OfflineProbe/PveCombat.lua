-- Preserve the original set ability for player targets. Its hit stack is
-- keyed by online account ID, so keep a separate stack for monster actors.
local M={}
local listener,owner,next_check,secondary_events
local path="/Game/TGF/Equipment/Gearset/ThunderLaw/GameAbility/"
local config
local verify=os.getenv("DB_NATIVE_PVE_SET_VERIFY")=="1"
local function load_class(name, directory)
    return assert(UE.UClass.Load((directory or path) .. name .. "." .. name .. "_C"),name)
end

local function thunder_law()
    if not config then
        local tag=UE.UTgfGameWidgetLibrary.GetGameplayTagFromString
        config={
            guarded=tag("TGF.Ability.Effect.Damage.Guarded"),
            cooldown=tag("TGF.Ability.CoolDown.ThunderLaw"),
            ability=load_class("GA_TGF_ThunderLaw"),
            damage=load_class("GE_TGF_Damage_Instant_ThunderLaw"),
            vulnerable=load_class("GE_TGF_Buff_ThunderLaw_Vulner"),
            slow=load_class("GE_TGF_Buff_ThunderLaw_ReduceSpeed"),
            cooldown_effect=load_class("GE_TGF_ThunderLaw_CoolDown"),
            mark=load_class("GE_TGF_ThunderLaw_ActiveMark"),
        }
    end
    return config
end

local function ability_instance(asc, class)
    local abilities=asc:GetAllActiveGameplayAbilitys()
    for i=1,abilities:Num() do
        local ability=abilities:Get(i)
        if ability and ability:GetClass()==class then return ability end
    end
end

local function hostile_monster(pawn, target)
    if not target or not target:IsA(UE.ALyraCharacter)
        or not UE.UEnumUtils.IsMonster(target:GetActorType()) then return false end
    local teams=UE.USubsystemBlueprintLibrary.GetWorldSubsystem(pawn,UE.ULyraTeamSubsystem)
    return teams and teams:CompareTeams(pawn,target)==UE.ELyraTeamComparison.DifferentTeams
end

local function is_player_source(pawn, source)
    return source and (source==pawn or source==pawn:GetLyraPlayerState())
end

local function active_hit(pawn, payload)
    local effect=payload.OptionalObject
    return is_player_source(pawn,payload.Instigator) and effect and effect:IsA(UE.UTgfGameplayEffect)
        and effect.ActiveType==0
end

local function on_damage(pawn, asc, ability, task, stacks, payload, mark)
    if verify then mark("Thunder Law probe received native damage event") end
    local target=payload.Target
    if not active_hit(pawn,payload) or not hostile_monster(pawn,target) then return end
    local attributes=UE.UTgfAttributeComponent.FindAttributeComponent(target)
    if not attributes or attributes:GetDeathState()~=0 then return end
    local sets=thunder_law()
    if asc:GetGameplayTagCount(sets.cooldown)>0
        or UE.UBlueprintGameplayTagLibrary.HasTag(payload.TargetTags,sets.guarded,false) then return end
    if ability.DamageTask~=task then return end
    local level=ability:GetAbilityLevel()
    local hits=ability.LevelRequiredHits:Get(level)
    if not hits or hits<1 then return end
    local now=UE.UGameplayStatics.GetTimeSeconds(pawn)
    local duration=ability.MaxDuration
    for actor,times in pairs(stacks) do
        if not UE.UKismetSystemLibrary.IsValid(actor) or now-times[#times]>duration then stacks[actor]=nil end
    end
    local times=stacks[target] or {}
    while times[1] and now-times[1]>duration do table.remove(times,1) end
    times[#times+1]=now
    stacks[target]=times
    if #times<hits then return end

    -- Keep the original cooldown watcher and rearm path. Finish the
    -- current task once the original effect and cooldown have applied.
    for actor in pairs(stacks) do stacks[actor]=nil end
    ability.GameplayEffectContextHandler=ability:GetContextFromOwner(UE.FGameplayAbilityTargetDataHandle())
    local data=UE.UAbilitySystemBlueprintLibrary.AbilityTargetDataFromActor(target)
    ability:BP_ApplyGameplayEffectToTarget(data,sets.damage,1,1)
    if level>=2 then ability:BP_ApplyGameplayEffectToTarget(data,sets.vulnerable,1,1) end
    if level>=3 then ability:BP_ApplyGameplayEffectToTarget(data,sets.slow,1,5) end
    asc:BP_ApplyGameplayEffectToSelf(sets.cooldown_effect,0,UE.FGameplayEffectContextHandle())
    if asc:GetGameplayTagCount(sets.cooldown)>0 then
        task:EndTask()
        ability.DamageTask=nil
        ability.DamageTimeStack:Clear()
        asc:RemoveActiveGameplayEffectBySourceEffect(sets.mark,nil,-1)
    end
end

local function configure_kleptomancy()
    local tag=UE.UTgfGameWidgetLibrary.GetGameplayTagFromString
    local tags=UE.TArray(UE.FGameplayTag)
    for _,name in ipairs({"PerfectGuarded","Guarded.LeftGuard","Guarded.RightGuard"}) do
        tags:Add(tag("TGF.Ability.Effect.Damage." .. name))
    end
    return {
        ability=load_class("GA_TGF_Kleptomancy",
            "/Game/TGF/Equipment/Gearset/Kleptomancy/GameplayAbility/"),
        blocked=UE.UBlueprintGameplayTagLibrary.MakeGameplayTagContainerFromArray(tags),
        melee=tag("TGF.AbilityHurtType.Melee"),
    }
end

local function kleptomancy(pawn, asc, sets, payload)
    if not active_hit(pawn,payload) or not hostile_monster(pawn,payload.Target)
        or UE.UBlueprintGameplayTagLibrary.HasAnyTags(payload.TargetTags,sets.blocked,false) then return end
    local ability=ability_instance(asc,sets.ability)
    if not ability or not ability.NotCD
        or not (ability.IsDisengaged or ability.DisengagedFrame==UE.UKismetSystemLibrary.GetFrameCount()) then return end
    if asc:GetGameplayTagCount(sets.melee)>0 then
        ability:DoTriggerRandomAbility(ability.TotalWeight_Melee,ability.RealAbilityConfig_Melee,payload.Target)
    else
        ability:DoTriggerRandomAbility(ability.TotalWeight,ability.RealAbilityConfig,payload.Target)
    end
end

local reset_talents={
    Talent_Wizard_Name2="TGF.Ability.Cooldown.ActiveSkill.Wizard.WizardE",
    Talent_DeathKnight_Name1="TGF.Ability.Cooldown.ActiveSkill.DeathKnight.DeathKnightE",
}
local function reset_skill_on_kill(pawn, asc, payload)
    if not is_player_source(pawn,payload.Instigator) or not hostile_monster(pawn,payload.Target) then return end
    local attribute=UE.UTgfAttributeComponent.FindAttributeComponent(pawn)
    local first=attribute and attribute:GetAttributeSet(UE.UTgfFirstLevelAttributeSet)
    if not first then return end
    local talents=first.Talents
    for i=1,talents:Num() do
        local talent=talents:Get(i)
        local cooldown=reset_talents[talent.Config.Name]
        if cooldown and talent.IsEnabled then
            local tag=UE.UTgfGameWidgetLibrary.GetGameplayTagFromString(cooldown)
            local tags=UE.UBlueprintGameplayTagLibrary.MakeGameplayTagContainerFromTag(tag)
            asc:RemoveActiveEffectsWithGrantedTags(tags)
            return
        end
    end
end

local function watch(pawn, event, callback)
    local tag=UE.UTgfGameWidgetLibrary.GetGameplayTagFromString(event)
    local action=assert(UE.UTgfGameplayEventAsyncAction.WaitForGameplayEventToCharaOrInteractor(pawn,pawn,tag))
    action.EventReceived:Add(pawn,function(_,_,payload) callback(payload) end)
    action:Activate()
    return action
end

local function stop_secondary_events()
    if not secondary_events then return end
    for _,action in pairs(secondary_events) do
        if UE.UKismetSystemLibrary.IsValid(action) then action.EventReceived:Clear() end
    end
    secondary_events=nil
end

function M.tick(gi, ready, mark)
    if not ready then
        stop_secondary_events()
        owner,listener,next_check=nil,nil,nil
        return
    end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if next_check and now<next_check then return end
    next_check=now+0.25
    local pawn=UE.UGameplayStatics.GetPlayerPawn(gi,0)
    if not pawn or not pawn:IsA(UE.ALyraCharacter) or not pawn:HasAuthority() then return end
    if pawn~=owner then
        stop_secondary_events()
        owner,listener=pawn,nil
    end
    local ok,err=pcall(function()
        local equipment=pawn:GetComponentByClass(UE.UTgfEquipmentManagerComponent)
        local asc=pawn:GetLyraAbilitySystemComponent()
        if not equipment or not asc then return end
        secondary_events=secondary_events or {}
        local role=pawn.ActorRoleTypeId
        if (role==400005 or role==400006) and not secondary_events.kill then
            secondary_events.kill=watch(pawn,"TGF.GameplayEvent.Killing",function(payload)
                local ok,fault=pcall(reset_skill_on_kill,pawn,asc,payload)
                if not ok then mark("native PvE talent cooldown reset: " .. tostring(fault)) end
            end)
        end
        if equipment:GetGearsetLevel(250000102)>=8 and not secondary_events.kleptomancy then
            local sets=configure_kleptomancy()
            secondary_events.kleptomancy=watch(pawn,"TGF.GameplayEvent.CausingDamage",function(payload)
                local ok,fault=pcall(kleptomancy,pawn,asc,sets,payload)
                if not ok then mark("native PvE Kleptomancy: " .. tostring(fault)) end
            end)
        end
        if equipment:GetGearsetLevel(250000104)<10 then return end
        local sets=thunder_law()
        local ability=ability_instance(asc,sets.ability)
        local task=ability and ability.DamageTask
        if not task or task==listener then return end
        local stacks={}
        task.EventReceived:Add(pawn,function(_,payload)
            if listener~=task then return end
            local success,fault=pcall(on_damage,pawn,asc,ability,task,stacks,payload,mark)
            if not success then
                mark("native Thunder Law PvE: " .. tostring(fault))
            end
        end)
        listener=task
        if verify then mark("Thunder Law probe listening on original damage task " .. tostring(task)) end
    end)
    if not ok then mark("native Thunder Law PvE listener: " .. tostring(err)) end
end

return M
