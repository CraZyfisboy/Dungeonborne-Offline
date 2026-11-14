-- original-client-loopback-handshake-local-server-entry-20261111-b
local mark=OfflineProbeContext.mark
local sync=require("OfflineProbe.SessionSync")
local potion_stacks=require("OfflineProbe.PotionStacks")
local pve_combat=require("OfflineProbe.PveCombat")
local map_portals=require("OfflineProbe.MapPortals")
local map_progression=require("OfflineProbe.MapProgression")
local map_chests=require("OfflineProbe.MapChests")
local map_bosses=require("OfflineProbe.MapBosses")
local floor_loot=require("OfflineProbe.FloorLoot")
local loot_visuals=require("OfflineProbe.LootVisuals")
local equipment_visuals=require("OfflineProbe.EquipmentVisuals")
sync.configure(mark)
local connect=assert(package.loadlib("./offline-native-adapter.dll","offline_adapter_connect"))
local poll=assert(package.loadlib("./offline-native-adapter.dll","offline_adapter_poll"))
local detach=assert(package.loadlib("./offline-native-adapter.dll","offline_adapter_detach"))
local timer=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UTimerSubsystem)
local seen_action=nil
-- This bridge file survives a previous client process. A new process must
-- consume only a new request issued by the current local lobby session.
do
    local previous=io.open("offline-native-action.txt","rb")
    if previous then
        seen_action=previous:read(1025)
        previous:close()
    end
end
local selected_map_id=nil
local initialized_map_pawn=nil
local pending_map=nil
local local_connected=false
local previous_level=""
local visited_battle=false
local function select_local_server()
    local servers=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.USelectServerService)
    local item=servers:GetCurrentServerItem()
    if not item then
        item=(NewObject or UE.NewObject)(UE.UServerItem,servers)
        item.ServerId=1
        item.CategoryId=1
        servers.Servers:Add(1,item)
    end
    item.ServerName="本机单人存档"
    servers:SetCurrentServerItem(item)
end
local function request_local_login(reason)
    select_local_server()
    local network=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UTGFNetworkManager)
    network:OnGamePreLoginSuccess(nil)
    mark("requested original game login through local endpoint: " .. reason)
end
local function item_buffer(item)
    local bytes=item.DItemBuffer.Buffer
    local chunks={}
    for i=1,bytes:Num() do chunks[i]=string.char(bytes:Get(i)) end
    return table.concat(chunks)
end

local function generate_market_stock()
    local gi=UE.UTGFBaseFunctionLibrary.GetGameInstance()
    local owner=UE.UGameplayStatics.GetPlayerPawn(gi,0) or UE.UGameplayStatics.GetPlayerController(gi,0)
    if not owner then return false end
    local types=assert(io.open("offline-market-stock-ids.txt","r"))
    local item_ids={}
    for line in types:lines() do
        local id=assert(tonumber(line),"market stock ID is not numeric")
        assert(id==math.floor(id) and id>0 and id<=2147483647 and #item_ids<1024,"market stock ID is out of range")
        item_ids[#item_ids+1]=id
    end
    types:close()
    assert(#item_ids>0,"market stock list is empty")
    local parts={"DBTRADE1"}
    local generated=0
    for _,id in ipairs(item_ids) do
        local items=UE.UBattleUtility.CreateItemInstanceForLobby(owner,id,1,"")
        if items:Num()==1 then
            local bytes=item_buffer(items:Get(1))
            if #bytes>0 and #bytes<=65536 then
                parts[#parts+1]=string.pack("<I4I4",id,#bytes)
                parts[#parts+1]=bytes
                generated=generated+1
            end
        end
    end
    assert(generated>0,"original client produced no stock items")
    local stock=assert(io.open("offline-market-stock-install.tmp","wb"))
    assert(stock:write(table.concat(parts)))
    assert(stock:close())
    assert(os.rename("offline-market-stock-install.tmp","offline-market-stock-install.bin"))
    mark("installed virtual market stock: " .. generated .. "/" .. #item_ids)
    return true
end
local function process_local_action()
    local file=io.open("offline-native-action.txt","rb")
    if not file then return end
    local data=file:read(1025)
    file:close()
    if not data or #data > 1024 or data == seen_action then return end
    seen_action=data
    local header,kind,id,map,map_id,timeout,multiplier,entry=data:match("^([^\n]+)\n([^\n]+)\n([^\n]+)\n([^\n]+)\n(%d+)\n(%d+)\n(%d+)\n([^\n]+)\n$")
    if header ~= "DBACTION1" or kind ~= "start_battle" or not id:match("^[a-f0-9]+$") or #id ~= 32
        or not map:match("^/TgfMaps/Scenes/[%w_/]+$") or not entry:match("^/TgfMaps/Scenes/[%w_/]+$")
        or tonumber(multiplier) < 1 or tonumber(multiplier) > 10 then
        mark("rejected invalid local action data")
        return
    end
    selected_map_id=tonumber(map_id)
    sync.arm(id)
    map_progression.arm()
    map_bosses.arm()
    local start_map=os.getenv("DB_NATIVE_WAITING_ENTRY") == "1" and entry or map
    pending_map={name=start_map:match("([^/]+)$"),id=selected_map_id,timeout=tonumber(timeout),ready=false}
    local pending=pending_map
    timer:AddTimer(timer,function() if pending_map == pending then pending.ready=true end end,5.0,false,5.0)
    local battle=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UBattleService)
    mark("original lobby beginning local run " .. id .. " scene=" .. map)
    battle:StartLocalBattle(start_map)
end
local poll_handle=timer:AddTimer(timer,function()
    poll()
    process_local_action()
    if local_connected then
        local gi=UE.UTGFBaseFunctionLibrary.GetGameInstance()
        local level=UE.UGameplayStatics.GetCurrentLevelName(gi,true)
        local floor=level:match("^L_Dungeon_Official_") or level:match("^L_GothicCastle_WP_Main")
        floor_loot.enter(level,selected_map_id)
        pve_combat.tick(gi,floor and pending_map==nil,mark)
        map_portals.tick(gi,level,floor and pending_map==nil,mark)
        map_progression.tick(gi,level,floor and pending_map==nil,mark)
        map_chests.tick(gi,floor and pending_map==nil)
        map_bosses.tick(gi,level,floor and pending_map==nil,mark)
        loot_visuals.tick(gi,floor and pending_map==nil,mark)
        equipment_visuals.tick(gi,floor and pending_map==nil,mark)
        sync.tick(gi,level,pending_map==nil)
        if floor then
            visited_battle=true
        elseif level == "Login" and previous_level ~= "Login" and visited_battle then
            visited_battle=false
            request_local_login("return from local dungeon")
        end
        previous_level=level
    end
    if pending_map and pending_map.ready then
        local gi=UE.UTGFBaseFunctionLibrary.GetGameInstance()
        local pawn=UE.UGameplayStatics.GetPlayerPawn(gi,0)
        local controller=UE.UGameplayStatics.GetPlayerController(gi,0)
        if controller and UE.UGameplayStatics.GetCurrentLevelName(gi,true) == pending_map.name then
            local pending=pending_map
            pending_map=nil
            local utility=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UBattleDsUtility)
            mark("native local map rules initialization: " .. tostring(utility:InitGameMap(pending.id,pending.timeout,true)))
            utility:OnMapFinish()
            potion_stacks.apply()
            mark("native local map advanced to battle floor, loot map=" .. tostring(pending.id))
            local matching=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UMatchService)
            matching:ExitMatching()
        end
    end
end,0.05,true,0.05)
timer:AddTimer(timer,function()
    select_local_server()
    connect()
    local f=io.open("offline-adapter-attached.txt","rb")
    if not f then mark("local adapter abstained");return end
    f:close()
    local_connected=true
    potion_stacks.apply(mark)
    request_local_login("initial startup")
end,3.0,false,3.0)
timer:AddTimer(timer,function()
    local login=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UGameLoginService)
    mark("original client local-login flag: " .. tostring(login:IsLogin()))
    mark("original client local-open-id: " .. tostring(login:BP_GetOpenId()))
    mark("original client local-map: " .. tostring(UE.UGameplayStatics.GetCurrentLevelName(UE.UTGFBaseFunctionLibrary.GetGameInstance(),true)))
    local inventory=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UInventoryService)
    mark("original client cached gold: " .. tostring(inventory:GetCoin()))
end,7.0,false,7.0)
if os.getenv("DB_ADAPTER_KEEP_RUNNING") ~= "1" then
timer:AddTimer(timer,function()
    timer:RemoveTimer(poll_handle)
    detach()
    mark("loopback adapter explicitly detached after bounded verification")
end,tonumber(os.getenv("DB_ADAPTER_VERIFY_TIMEOUT") or "22"),false,tonumber(os.getenv("DB_ADAPTER_VERIFY_TIMEOUT") or "22"))
end
if os.getenv("DB_NATIVE_PRODUCT") == "1" then
    if os.getenv("DB_LOCAL_INSTALL_STOCK") == "1" then
        local handle
        handle=timer:AddTimer(timer,function()
            local gi=UE.UTGFBaseFunctionLibrary.GetGameInstance()
            local login=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UGameLoginService)
            if UE.UGameplayStatics.GetCurrentLevelName(gi,true)=="RuinedCrypt_03" and login:IsLogin() then
                local ok,complete=pcall(generate_market_stock)
                if not ok or complete then timer:RemoveTimer(handle) end
                if not ok then mark("virtual market stock preparation failed: " .. tostring(complete)) end
            end
        end,0.5,true,0.5)
    end
    return "original offline client started"
end
