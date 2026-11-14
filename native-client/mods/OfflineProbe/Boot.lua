-- Research bootstrap and guarded local gameplay on the original Lua thread.
local M = {}

function M.start(mark)
    local route = os.getenv("DB_NATIVE_BOOT_ROUTE") or "original-lobby"
    local product = os.getenv("DB_NATIVE_PRODUCT") == "1"
    local function check(label, fn)
        local ok, value = pcall(fn)
        mark(label .. ": " .. (ok and tostring(value) or ("ERROR " .. tostring(value))))
        return ok and value or nil
    end
    check("engine version", function() return UE.UKismetSystemLibrary.GetEngineVersion() end)
    mark("CurrentRunMode: " .. tostring(CurrentRunMode))
    local function observe(context, tag)
        local gi = check(tag .. " game instance", function() return UE.UTGFBaseFunctionLibrary.GetGameInstance() end)
        context = gi or context
        if context then
            check(tag .. " map", function() return UE.UGameplayStatics.GetCurrentLevelName(context, true) end)
            check(tag .. " game mode", function() return UE.UGameplayStatics.GetGameMode(context) end)
            check(tag .. " player controller", function() return UE.UGameplayStatics.GetPlayerController(context, 0) end)
            check(tag .. " pawn", function() return UE.UGameplayStatics.GetPlayerPawn(context, 0) end)
            check(tag .. " dedicated", function() return UE.UKismetSystemLibrary.IsDedicatedServer(context) end)
            check(tag .. " characters", function()
                local actors = UE.TArray(UE.AActor)
                UE.UGameplayStatics.GetAllActorsOfClass(context, UE.ACharacter, actors)
                local names = {}
                for i = 1, math.min(actors:Num(), 12) do names[#names + 1] = tostring(actors:Get(i)) end
                return tostring(actors:Num()) .. " [" .. table.concat(names, "; ") .. "]"
            end)
            check(tag .. " AI controllers", function()
                local actors = UE.TArray(UE.AActor)
                UE.UGameplayStatics.GetAllActorsOfClass(context, UE.AAIController, actors)
                return actors:Num()
            end)
            local pawn = UE.UGameplayStatics.GetPlayerPawn(context, 0)
            if pawn then
                check(tag .. " position", function()
                    local v = pawn:K2_GetActorLocation()
                    return tostring(v.X) .. ", " .. tostring(v.Y) .. ", " .. tostring(v.Z)
                end)
                check(tag .. " health", function()
                    local h = UE.ULyraHealthComponent.FindHealthComponent(pawn)
                    if not h then return "no health component" end
                    return tostring(h:GetHealth()) .. "/" .. tostring(h:GetMaxHealth())
                end)
            end
            check(tag .. " battle state", function()
                local b = UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UBattleDsUtility)
                if not b then return "no battle utility" end
                return "room ready=" .. tostring(b:IsRoomReady()) .. " options ready=" .. tostring(b:IsOptionArgsInit())
            end)
            check(tag .. " world streaming complete", function()
                local s = UE.USubsystemBlueprintLibrary.GetWorldSubsystem(context, UE.UWorldPartitionSubsystem)
                return s and s:IsAllStreamingCompleted()
            end)
            if pawn then
                check(tag .. " original character data", function()
                    return "role=" .. tostring(pawn.ActorRoleTypeId) .. " level=" .. tostring(pawn.ActorLevel) .. " career=" .. tostring(pawn.ActorCareer)
                end)
                check(tag .. " original attributes", function()
                    local a = UE.UTgfAttributeComponent.FindAttributeComponent(pawn)
                    if not a then return "missing" end
                    local d = a:GetAttributeSet(UE.UTgfDefendAttributeSet)
                    if not d then return "no defend set" end
                    return tostring(d.CurHealth.CurrentValue) .. "/" .. tostring(d:GetMaxHealthFinal())
                end)
                check(tag .. " capsule collision", function()
                    local c = pawn.CapsuleComponent
                    if not c then return "no capsule in current level" end
                    return "profile=" .. tostring(c:GetCollisionProfileName()) .. " enabled=" .. tostring(c:GetCollisionEnabled()) .. " static response=" .. tostring(c:GetCollisionResponseToChannel(0))
                end)
            end
        end
    end
    observe(nil, "startup")
    local timer = require("Base.TimerSubsystem")
    local previous_init = timer.Lua_Initialized
    timer.Lua_Initialized = function(self, ...)
        mark("timer initialized")
        if previous_init then previous_init(self, ...) end
        observe(self, "timer init")
    end
    local previous_tick = timer.Lua_Tick
    local elapsed, next_observation = 0, 0
    local requested = false
    mark("bootstrap route: " .. route)
    local map_initialized = false
    local guarded_pawn, spawn, entered, released, last_trace = nil, nil, nil, false, -1
    local last_request, next_command_check = nil, 0
    local started_client = false
    _G.OfflineProbeContext = { mark = mark, check = check, observe = observe }
    timer.Lua_Tick = function(self, delta)
        elapsed = elapsed + delta
        local gi = UE.UTGFBaseFunctionLibrary.GetGameInstance()
        local level_name=gi and UE.UGameplayStatics.GetCurrentLevelName(gi,true) or ""
        if gi and (level_name:match("^L_Dungeon_Official_0[12]") or level_name:match("^L_GothicCastle_WP_Main")) then
            local pawn = UE.UGameplayStatics.GetPlayerPawn(gi, 0)
            if pawn and pawn ~= guarded_pawn and UE.UTgfAttributeComponent.FindAttributeComponent(pawn) then
                guarded_pawn, released, entered = pawn, false, elapsed
                local v = pawn:K2_GetActorLocation()
                spawn = UE.FVector(v.X, v.Y, v.Z + 100)
                check("hold original pawn during streaming", function()
                    pawn:GetMovementComponent():DisableMovement()
                    pawn:K2_SetActorLocation(spawn, false, UE.FHitResult(), true)
                    return tostring(spawn.X) .. ", " .. tostring(spawn.Y) .. ", " .. tostring(spawn.Z)
                end)
            end
            if pawn and spawn and not released and elapsed - entered > 2 and math.floor(elapsed) ~= last_trace then
                last_trace = math.floor(elapsed)
                check("spawn ground trace", function()
                    local ignored = UE.TArray(UE.AActor)
                    ignored:Add(pawn)
                    local hit = UE.FHitResult()
                    local result = UE.UKismetSystemLibrary.LineTraceSingle(gi,
                        UE.FVector(spawn.X, spawn.Y, spawn.Z + 100),
                        UE.FVector(spawn.X, spawn.Y, spawn.Z - 700),
                        0, false, ignored, 0, hit, true,
                        UE.FLinearColor(1, 0, 0, 1), UE.FLinearColor(0, 1, 0, 1), 0)
                    if result then
                        pawn:GetMovementComponent():SetMovementMode(1, 0)
                        released = true
                        mark("released pawn after ground collision confirmed")
                    end
                    return tostring(result) .. " hit z=" .. tostring(hit.ImpactPoint.Z)
                end)
            end
        end
        if product and not started_client then
            started_client = true
            check("start original offline client",function() return require("OfflineProbe.Client") end)
        elseif not product and elapsed >= next_command_check then
            next_command_check = elapsed + 1
            local file = io.open("offline-probe-command.lua", "rb")
            if file then
                local request = file:read("*a")
                file:close()
                if request ~= last_request then
                    last_request = request
                    local fn, err = load(request, "@OfflineProbeCommand.lua", "t", _ENV)
                    if not fn then mark("command compile failed: " .. tostring(err))
                    else check("diagnostic command", fn) end
                end
            end
        end
        if not product and elapsed >= next_observation and next_observation <= 30 then
            next_observation = next_observation + 5
            observe(self, "tick " .. tostring(math.floor(elapsed)))
        end
        if not requested and elapsed >= 3 and route ~= "original-lobby" then
            requested = true
            local gi = check("travel context", function() return UE.UTGFBaseFunctionLibrary.GetGameInstance() end)
            if gi then
                check("close original login window", function()
                    local wm = UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UTGFWindowManager)
                    return wm:CloseWindowBySoftPath("/Game/TGF/UI/WBP/WBP_LoginWindow.WBP_LoginWindow_C")
                end)
                check("request original Crypt level", function()
                    if route == "builtin-local" then
                        local b = UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UBattleService)
                        b:StartBeginnerLevel("/TgfMaps/Scenes/Dungeon/L_Dungeon_Official_01", false)
                    else
                        UE.UGameplayStatics.OpenLevel(gi, "/TgfMaps/Scenes/Dungeon/L_Dungeon_Official_01", true, "")
                    end
                    return "requested, not yet verified"
                end)
            end
        end
        if not map_initialized and elapsed >= 12 and route == "direct-travel" then
            map_initialized = true
            check("initialize original battle config", function()
                local b = UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UBattleDsUtility)
                if not b then return "no battle utility" end
                return b:InitGameMap(1, 2400, true)
            end)
        end
        if previous_tick then return previous_tick(self, delta) end
    end
    mark("timer observations installed")
end

return M
