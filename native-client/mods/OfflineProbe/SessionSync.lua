-- Fixed local events. Only the original map/role state provides run outcomes.
local M={}
local directory=os.getenv("DB_NATIVE_EVENT_DIR") or ""
if not directory:match("^offline%-events%-%x+$") or #directory ~= 39 then directory=nil end
M.available=directory~=nil
local action_counter,action_wait=0,{}
local run
local return_at=nil
local mark=function() end
local last_capture, capture_error=0,nil
local inventory_revision,next_revision_check=nil,0
local applied_inventory_revision=0
local pending_inventory=nil
local inventory_sync=require("OfflineProbe.InventorySync")

local function write_atomic(name,data)
    if not directory then return false end
    local path=directory .. "/" .. name
    local file=io.open(path .. ".tmp","wb")
    if not file then return false end
    local ok=file:write(data)
    local closed=file:close()
    return ok ~= nil and closed and os.rename(path .. ".tmp",path)
end

local function read(name,maximum)
    if not directory then return nil end
    local file=io.open(directory .. "/" .. name,"rb")
    if not file then return nil end
    local data=file:read(maximum)
    file:close()
    return data
end

function M.configure(logger) mark=logger or mark end

local function refresh_lobby_inventory(gi)
    local lobby=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.ULobbyService)
    if lobby then lobby:SendPlayerGetInfoReq() end
end

local function sync_inventory(gi,now)
    local current=read("inventory-revision.txt",32)
    if current and current~=inventory_revision and current:match("^%d+\n$") then
        inventory_revision=current
        local previous=pending_inventory
        pending_inventory={last=tonumber(current),retry_at=previous and previous.retry_at or now,
                           snapshot_revision=previous and previous.snapshot_revision}
    end
    local pending=pending_inventory
    if not pending then return end
    if not pending.data then
        pending.data=inventory_sync.prepare(read,applied_inventory_revision+1,pending.last)
    end
    if not pending.data then return end
    if pending.data.needs_snapshot and pending.snapshot_revision~=pending.last then
        refresh_lobby_inventory(gi)
        pending.snapshot_revision=pending.last
        pending.retry_at=now+0.5
        return
    end
    local ok,applied,count,cleared=pcall(inventory_sync.apply,pending.data)
    if ok and applied then
        applied_inventory_revision=pending.last
        pending_inventory=nil
        write_atomic("inventory-cache-" .. pending.last .. ".ack","OK\n")
        mark("reconciled original inventory cells, grids=" .. tostring(count) .. " cleared=" .. tostring(cleared))
        return
    end
    if not ok and pending.error~=applied then
        pending.error=applied
        mark("original inventory cache refresh: " .. tostring(applied))
    end
    if now>=pending.retry_at then
        refresh_lobby_inventory(gi)
        pending.retry_at=now+0.5
    end
end

function M.action(method,pages,callback)
    if method~="status" and method~="warehouse" and method~="backpack" then return false end
    action_counter=action_counter+1
    local name="action-" .. action_counter
    if not write_atomic(name .. ".txt","DBACT01\n" .. method .. "\n" .. tostring(pages or 0) .. "\n") then
        if callback then callback(nil,"本机存档服务暂不可用") end
        return false
    end
    action_wait[name]={callback=callback,deadline=os.time()+10}
    return true
end

function M.arm(id)
    if not directory then return end
    run={id=id,entered=false}
    return_at=nil
    last_capture,capture_error=0,nil
end

local function item_entry(item,storage,x,y)
    local count=item:GetStackCount()
    if count==0 then return nil end
    local bytes=item.DItemBuffer.Buffer
    local out={}
    for j=1,bytes:Num() do out[j]=string.char(bytes:Get(j)) end
    local data=table.concat(out)
    assert(#data>0 and #data<=65536,"original item protobuf is unavailable")
    return string.pack("<I1i2i2I4I4",storage,x,y,count,#data) .. data
end

local function capture(pawn)
    local bag=pawn:GetComponentByClass(UE.UTgfInventoryManagerComponent)
    local equipment=pawn:GetComponentByClass(UE.UTgfEquipmentManagerComponent)
    assert(bag and equipment,"original role inventory is unavailable")
    local pieces,seen={},{}
    local items=bag:GetAllItems()
    local pouch=bag:GetSize(1)
    for i=1,items:Num() do
        local item=items:Get(i)
        local point=UE.FIntPoint()
        local found=bag:GetPositionByItemInstance(item,0,point)
        assert(found,"original backpack item has no grid position")
        local storage=2
        if pouch and pouch.X>0 and pouch.Y>0 and point.X<pouch.X and point.Y<pouch.Y
            and bag:GetItemInstanceByPos(point,1)==item then storage=4 end
        pieces[#pieces+1]=item_entry(item,storage,point.X,point.Y)
        seen[tostring(item)]=true
    end
    local worn=equipment:GetAllEquipments(false)
    for i=1,worn:Num() do
        local entry=worn:Get(i)
        local item=entry.InventoryInstance
        local slot=equipment:GetSlotOfEquipment(entry)
        if item and not seen[tostring(item)] and slot~=0 and slot~=100 then
            pieces[#pieces+1]=item_entry(item,3,slot,0)
            seen[tostring(item)]=true
        end
    end
    return pieces
end

local function save_outcome(kind)
    local items=kind==3 and {} or run.snapshot
    if not items then return false end
    if #items>2048 then error("native inventory exceeds the supported snapshot count") end
    local name="outcome-" .. run.id
    if write_atomic(name .. ".bin","DBRUN02\n" .. run.id .. string.pack("<I1I2I4",kind,#items,run.played_seconds or 0) ..
        table.concat(items)) then
        run.sent=name
        run.outcome=kind
        mark("native run outcome submitted: " .. run.id .. " kind=" .. kind .. " items=" .. #items)
        return true
    end
    return false
end

function M.tick(gi,level,ready)
    if not directory then return end
    for name,request in pairs(action_wait) do
        local data=read(name .. ".ack",1024)
        if data or os.time()>=request.deadline then
            action_wait[name]=nil
            local pages,cost,gold,warehouse=(data or ""):match("^OK\n(%d+)\n(%d+)\n(%d+)\n(%d+)\n$")
            if pages then
                M.status={pages=tonumber(pages),next_cost=tonumber(cost),gold=tonumber(gold),warehouse=tonumber(warehouse)}
                if request.callback then request.callback(M.status) end
            elseif request.callback then request.callback(nil,(data or ""):match("^ERROR\n([^\n]+)") or "本机存档没有响应") end
        end
    end
    if level=="RuinedCrypt_03" then
        local now=UE.UGameplayStatics.GetRealTimeSeconds(gi)
        if now>=next_revision_check and not UE.UWidgetBlueprintLibrary.IsDragDropping() then
            next_revision_check=now+0.05
            sync_inventory(gi,now)
        end
    else
        next_revision_check=0
        pending_inventory=nil
        applied_inventory_revision=tonumber(inventory_revision or "") or applied_inventory_revision
    end
    local is_floor=level:match("^L_Dungeon_Official_") or level:match("^L_GothicCastle_WP_Main")
    if return_at then
        if not is_floor then
            return_at=nil
        elseif os.time()>=return_at then
            return_at=nil
            local matching=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UMatchService)
            if matching:GetResultData() then matching:SetShowBattleResult(true) end
            UE.UGameplayStatics.OpenLevel(gi,"Empty",true,"")
            mark("returned through the original lobby level after local settlement")
            return
        end
    end
    if not run then return end
    if run.sent then
        local ack=read(run.sent .. ".ack",1024)
        if ack then
            mark("native run outcome acknowledgement: " .. (ack:match("^[^\n]+") or "invalid") .. " " .. run.id)
            if ack~="OK\n" then
                local why=ack:match("^ERROR\n([^\n]+)") or "未知错误"
                UE.UTgfUIBlueprintLibrary.ShowToast("本局保存失败：" .. why)
            elseif os.getenv("DB_NATIVE_PRODUCT")=="1" and run.outcome~=3 and is_floor then
                return_at=os.time()+(run.outcome==1 and 3 or 8)
            end
            run=nil
        end
        return
    end
    if not is_floor then
        if run.entered and (level=="Empty" or level=="Login" or level=="RuinedCrypt_03") then save_outcome(3) end
        return
    end
    if not ready then return end
    local now=UE.UGameplayStatics.GetTimeSeconds(gi)
    if level~=run.level then
        run.level=level
        run.finished_floor_seconds=run.played_seconds or 0
        run.floor_start=now
        last_capture=-1
    end
    run.played_seconds=run.finished_floor_seconds+math.max(0,math.floor(now-run.floor_start))
    local pc=UE.UGameplayStatics.GetPlayerController(gi,0)
    local pawn=UE.UGameplayStatics.GetPlayerPawn(gi,0)
    local state=pc and pc.PlayerState
    if not state then return end
    local attr=pawn and pawn:GetComponentByClass(UE.UTgfAttributeComponent)
    local death=attr and attr:GetDeathState()
    local utility=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem(UE.UBattleDsUtility)
    local finished=utility:IsPlayerGameOver(state)
    if finished and (death==4 or state:GetIsEscapeQuit()) then
        save_outcome(1)
    elseif finished and (death==1 or death==2 or state:GetIsDeathQuit()) then
        save_outcome(2)
    elseif pawn and pawn:HasAuthority() and death==0 and now-last_capture>=0.25 then
        last_capture=now
        local ok,items=pcall(capture,pawn)
        if ok then
            run.snapshot,run.entered=items,true
            capture_error=nil
        elseif tostring(items)~=capture_error then
            capture_error=tostring(items)
            mark("native inventory snapshot pending: " .. capture_error)
        end
    end
end

return M
