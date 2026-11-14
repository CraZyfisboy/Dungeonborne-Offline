-- Reconcile original cell caches after the authoritative item snapshot arrives.
local M={}
local format="<I4I1I8i2i2I2I2I1I8i2i2I4"

function M.prepare(read,first,last)
    local pending={expected={},cells={}}
    for revision=first,last do
        local data=read("inventory-update-" .. revision .. ".bin",512*1024)
        if not data or #data<16 or data:sub(1,8)~="DBGRID1\n" then return nil end
        local number,count,at=string.unpack("<I4I4",data,9)
        if number~=revision or #data~=16+count*string.packsize(format) then return nil end
        if count==0 then pending.needs_snapshot=true end
        for i=1,count do
            local guid,kind,container,x,y,rows,columns,new_kind,new_container,new_x,new_y,new_count,next_offset=
                string.unpack(format,data,at)
            at=next_offset
            if kind>4 or new_kind>4 then return nil end
            pending.expected[guid]={kind=new_kind,container=new_container,x=new_x,y=new_y,count=new_count,
                                    previous_kind=kind,previous_container=container}
            if kind==1 or kind==2 or kind==4 then
                pending.cells[#pending.cells+1]={guid=guid,kind=kind,container=container,x=x,y=y,rows=rows,columns=columns}
            end
        end
    end
    return pending
end

function M.apply(pending)
    local subsystem=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem
    local inventory=subsystem(UE.UInventoryService)
    local player_info=subsystem(UE.UPlayerInfoService)
    local selected=player_info:GetHallRoleGuid()
    if not selected or selected==0 then return false end
    for guid,expected in pairs(pending.expected) do
        local item=UE.FTGFItem()
        local role=expected.kind==1 and 0 or expected.container
        local found=false
        if expected.kind==0 then
            role=expected.previous_kind==1 and 0 or expected.previous_container
        end
        if expected.kind==3 then
            local record=UE.FTGFUserRole()
            if player_info:GetRole(role,record) then
                for i=1,record.wearing_equips:Num() do
                    local candidate=record.wearing_equips:Get(i)
                    if candidate.guid==guid then item,found=candidate,true;break end
                end
            end
        else
            found=inventory:GetItemByGuid(role,guid,item)
        end
        if not found and role==0 then found=inventory:GetItemByGuid(selected,guid,item) end
        if expected.kind==0 then
            if found then return false end
        else
            local position=item.position
            if not found or item.count~=expected.count or position.type~=expected.kind or
                position.guid~=expected.container or position.x~=expected.x or position.y~=expected.y then
                return false
            end
        end
    end
    local cleared=0
    for _,old in ipairs(pending.cells) do
        local final=pending.expected[old.guid]
        local still_here=old.kind==final.kind and old.container==final.container and old.x==final.x and old.y==final.y
        if not still_here and (old.kind==1 or old.container==selected) then
            local page=old.kind==1 and old.container or 0
            local item=UE.FTGFItem()
            if inventory:GetCellItem(old.kind,old.x,old.y,page,item) and item.guid==old.guid then
                inventory:ResetCell(old.kind,UE.FIntVector(old.x,old.y,page),UE.FIntPoint(old.rows,old.columns))
                cleared=cleared+1
            end
        end
    end
    if next(pending.expected) then
        inventory:SetCurrentRole(0)
        inventory:SetCurrentRole(selected)
    end
    local refreshed=require("OfflineProbe.StorageUI").refresh(pending.expected,selected)
    return true,refreshed,cleared
end

return M
