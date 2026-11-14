-- Adds controls to the original warehouse and character inventory widgets.
local M={}
local sync=require("OfflineProbe.SessionSync")
local open_grids={}
local function backpack_context(typ)
    local kind=UE.ETgfInventoryType
    return typ~=nil and (typ==kind.Lobby or typ==kind.Market or typ==kind.Trade or
           typ==kind.TradeNoRight or typ==kind.TradeAuction or typ==kind.TradeBuyItNow)
end

local function grid_position(context,role)
    local typ=context.InventoryType
    local kind=UE.ETgfInventoryType
    if typ==kind.Storage then return 1,context.PageId end
    if typ==kind.SmallPack then return 4,role end
    if backpack_context(typ) then return 2,role end
end

local function matches(item,kind,container,position_kind,position_container,x,y)
    local point=item.InventoryPosition
    return kind==position_kind and container==position_container and point.X==x and point.Y==y
end

local function current_entry(entry)
    local grid,item=entry.OfflineInventoryGrid,entry.CachedItem
    if not grid or not item then return true end
    local guid=item:GetGUID()
    if not guid or guid==0 then return true end
    local context=grid.CachedInventoryContext
    if not context then return false end
    local subsystem=UE.UTGFBaseFunctionLibrary.GetGameInstanceSubsystem
    local role=subsystem(UE.UPlayerInfoService):GetHallRoleGuid()
    if not role or role==0 then return true end
    local kind,container=grid_position(context,role)
    if not kind then return true end
    local inventory=subsystem(UE.UInventoryService)
    local record=UE.FTGFItem()
    local found=inventory:GetItemByGuid(kind==1 and 0 or role,guid,record)
    if not found and kind==1 then found=inventory:GetItemByGuid(role,guid,record) end
    local position=record.position
    if found and matches(item,kind,container,position.type,position.guid,position.x,position.y) then return true end
    grid:RefreshEntries(true)
    return false
end

local function toast(message)
    UE.UTgfUIBlueprintLibrary.ShowToast(message)
end

local function trace(label,callback)
    local ok,err=pcall(callback)
    if not ok and OfflineProbeContext then OfflineProbeContext.mark(label .. ": " .. tostring(err)) end
end

local function set_small_text(widget,label)
    local font=widget.Font
    font.Size=17
    widget:SetFont(font)
    widget:SetText(label)
end

local function button(owner,label,fn)
    local new=NewObject or UE.NewObject
    local widget=new(UE.UButton,owner)
    local text=new(UE.UTextBlock,widget)
    set_small_text(text,label)
    widget:SetBackgroundColor(UE.FLinearColor(0.28,0.23,0.17,1))
    widget:AddChild(text)
    widget.OnClicked:Add(owner,fn)
    return widget
end

local function upgrade(owner)
    sync.action("status",0,function(status,err)
        if not status then toast(err);return end
        if status.next_cost==0 then toast("地牢背包已满级");return end
        local message=string.format("花费 %d 金币，把地牢背包从 %d 页升级为 %d 页？",status.next_cost,status.pages,status.pages+1)
        local confirm=UE.UAsyncAction_ShowConfirm.ShowConfirm(owner,message,"升级","取消",owner)
        if not confirm then toast("无法打开升级确认");return end
        confirm.OnResult:Add(owner,function(_,result)
            if result==UE.EConfirmResult.Confirmed then
                sync.action("backpack",status.pages,function(updated,problem)
                    if updated then toast(string.format("地牢背包已升级为 %d 页。重新打开背包可查看。",updated.pages))
                    else toast(problem) end
                end)
            end
        end)
        confirm:Activate()
    end)
end

local function setup_character(owner,context)
    local typ=context and context.InventoryType
    local kind=UE.ETgfInventoryType
    if not backpack_context(typ) and typ~=kind.InGameLocalPlayer then return end
    local slots=owner.WBP_TgfGame_InventorySlots
    if typ~=kind.InGameLocalPlayer then open_grids[slots]=true end
    local size=context:GetInventorySize()
    local pages=math.max(1,math.ceil(size.X/6))
    local view=owner.OfflineBackpackView
    if not view and (pages>1 or typ==UE.ETgfInventoryType.Lobby) then
        local overlay=slots:GetParent()
        if not overlay then return end
        local new=NewObject or UE.NewObject
        local layout=new(UE.UVerticalBox,owner)
        local box=new(UE.USizeBox,layout)
        local scroll=new(UE.UScrollBox,box)
        scroll:SetClipping(UE.EWidgetClipping.ClipToBoundsAlways)
        scroll:SetAllowOverscroll(false)
        scroll:SetScrollBarVisibility(UE.ESlateVisibility.Collapsed)
        scroll:SetConsumeMouseWheel(UE.EConsumeMouseWheel.Never)
        box:AddChild(scroll)
        layout:AddChildToVerticalBox(box)

        local bar=new(UE.UHorizontalBox,owner)
        local label=new(UE.UTextBlock,bar)
        set_small_text(label,"")
        local prev=button(owner," 上一页 ",function() M.page(owner,-1) end)
        local next_button=button(owner," 下一页 ",function() M.page(owner,1) end)
        prev:SetToolTipText("拖着物品移到此处，可翻到上一页")
        next_button:SetToolTipText("拖着物品移到此处，可翻到下一页")
        local upgrade_button=button(owner," 升级背包 ",function() upgrade(owner) end)
        for _, control in ipairs({prev,label,next_button,upgrade_button}) do
            local slot=bar:AddChildToHorizontalBox(control)
            slot:SetVerticalAlignment(UE.EVerticalAlignment.VAlign_Center)
            slot:SetPadding(UE.FMargin(4,0,4,0))
        end
        local row=layout:AddChildToVerticalBox(bar)
        row:SetPadding(UE.FMargin(0,6,0,0))
        row:SetHorizontalAlignment(UE.EHorizontalAlignment.HAlign_Center)

        -- The scroll area and controls occupy distinct rows of the same panel.
        -- Keep the original inventory's other overlay children in place.
        local place=overlay:AddChildToOverlay(layout)
        place:SetHorizontalAlignment(UE.EHorizontalAlignment.HAlign_Left)
        place:SetVerticalAlignment(UE.EVerticalAlignment.VAlign_Top)
        slots:RemoveFromParent()
        scroll:AddChild(slots)
        view={box=box,scroll=scroll,bar=bar,label=label,prev=prev,next=next_button,upgrade=upgrade_button,page=1}
        owner.OfflineBackpackView=view
    end
    if not view then return end
    local vis=UE.ESlateVisibility
    local _,height=slots:GetCellSize()
    height=tonumber(height)
    if not height or height<1 then height=tonumber(slots.OriginalCellSize) or 54 end
    if height<1 then height=54 end
    view.pages,view.step=pages,height*6
    view.drag_operation,view.drag_direction=nil,nil
    if pages>1 then
        view.box:SetHeightOverride(view.step)
    else
        view.box:ClearHeightOverride()
    end
    view.upgrade:SetVisibility(typ==UE.ETgfInventoryType.Lobby and vis.Visible or vis.Collapsed)
    view.prev:SetVisibility(pages>1 and vis.Visible or vis.Collapsed)
    view.next:SetVisibility(pages>1 and vis.Visible or vis.Collapsed)
    view.bar:SetVisibility((pages>1 or typ==UE.ETgfInventoryType.Lobby) and vis.SelfHitTestInvisible or vis.Collapsed)
    M.page(owner,0)
end

M.setup_character=setup_character

function M.page(owner,delta)
    local view=owner.OfflineBackpackView
    if not view then return end
    view.page=math.min(view.pages,math.max(1,view.page+delta))
    view.label:SetText(string.format(" 地牢背包 %d / %d ",view.page,view.pages))
    view.prev:SetIsEnabled(view.page>1)
    view.next:SetIsEnabled(view.page<view.pages)
    if view.scroll then view.scroll:SetScrollOffset((view.page-1)*view.step) end
end

function M.drag_page(owner,absolute,operation)
    local view=owner.OfflineBackpackView
    if not view or view.pages<2 then return false end
    local slate=UE.USlateBlueprintLibrary
    local direction=nil
    if slate.IsUnderLocation(view.prev:GetCachedGeometry(),absolute) then direction=-1
    elseif slate.IsUnderLocation(view.next:GetCachedGeometry(),absolute) then direction=1 end
    local now=UE.UGameplayStatics.GetRealTimeSeconds(owner)
    if direction and (operation~=view.drag_operation or direction~=view.drag_direction or now>=view.drag_repeat_at) then
        M.page(owner,direction)
        view.drag_repeat_at=now+0.8
    end
    view.drag_operation,view.drag_direction=operation,direction
    return direction~=nil
end

function M.refresh(expected,role)
    local refreshed=0
    for slot in pairs(open_grids) do
        if UE.UKismetSystemLibrary.IsValid(slot) then
            slot:RefreshEntries(true)
            local context=slot.CachedInventoryContext
            local kind,container
            if context then kind,container=grid_position(context,role) end
            if kind then
                for key,cell in pairs(slot.ItemCells or {}) do
                    local item=cell.ItemEntry and cell.ItemEntry.CachedItem
                    local committed=item and expected[item:GetGUID()]
                    if committed and not matches(item,kind,container,committed.kind,committed.container,committed.x,committed.y) then
                        cell:RemoveFromParent()
                        slot.ItemCells[key]=nil
                    end
                end
            end
            refreshed=refreshed+1
        else
            open_grids[slot]=nil
        end
    end
    return refreshed
end

function M.apply(class,module)
    if not sync.available then return class end
    if module=="BP_Common_ItemEntry" then
        local process=class.ShouldProcessMouseEvent
        local drag=class.DetectDrag
        function class:ShouldProcessMouseEvent()
            return process(self) and current_entry(self)
        end
        function class:DetectDrag()
            if current_entry(self) then return drag(self) end
        end
    elseif module=="BP_InventorySlot" then
        local setup=class.SetupWithContext
        local destruct=class.Destruct
        local refresh_entries=class.RefreshEntries
        local potion_stacks=require("OfflineProbe.PotionStacks")
        function class:SetupWithContext(context,gamepad)
            local result=setup(self,context,gamepad)
            local typ=context and context.InventoryType
            local kind=UE.ETgfInventoryType
            open_grids[self]=(typ==kind.Storage or typ==kind.SmallPack or backpack_context(typ)) and true or nil
            return result
        end
        function class:Destruct()
            open_grids[self]=nil
            return destruct(self)
        end
        function class:RefreshEntries(rebuild)
            local context=self.CachedInventoryContext
            potion_stacks.context(context)
            local result=refresh_entries(self,rebuild)
            if context and grid_position(context,0) then
                for _,cell in pairs(self.ItemCells or {}) do
                    if cell.ItemEntry then cell.ItemEntry.OfflineInventoryGrid=self end
                end
            end
            return result
        end
    elseif module=="BP_InventoryStorage" then
        local original=class.SetBagCount
        local select_page=class.OnClickPageTab
        local destruct=class.Destruct
        function class:OnClickPageTab(page)
            local result=select_page(self,page)
            if self.CurrentContext then open_grids[self.LootInventorySlots]=true end
            return result
        end
        function class:Destruct()
            local slots=self.LootInventorySlots
            if slots then open_grids[slots]=nil end
            return destruct(self)
        end
        function class:SetBagCount(count)
            self.MaxPage=2147483647
            local result=original(self,count)
            if count>8 and not self.OfflineWarehouseScroller then
                trace("offline warehouse scrolling",function()
                    local icons=self.DynamicBagIcons
                    local parent=icons:GetParent()
                    local new=NewObject or UE.NewObject
                    local size=new(UE.USizeBox,self)
                    local scroll=new(UE.UScrollBox,size)
                    size:SetWidthOverride(500)
                    scroll:SetOrientation(UE.EOrientation.Orient_Horizontal)
                    scroll:SetAllowOverscroll(false)
                    scroll:SetClipping(UE.EWidgetClipping.ClipToBoundsAlways)
                    size:AddChild(scroll)
                    parent:AddChildToHorizontalBox(size)
                    icons:RemoveFromParent()
                    scroll:AddChild(icons)
                    self.OfflineWarehouseScroller=scroll
                end)
            end
            return result
        end
    elseif module=="BP_InventoryPageAddTab" then
        function class:IsAddPageItemEnough() return true end
        function class:OnAddPage()
            if self.OfflineAddingPage then return end
            self.OfflineAddingPage=true
            sync.action("warehouse",0,function(status,err)
                self.OfflineAddingPage=false
                if status then toast("已新增一页局外仓库") else toast(err) end
            end)
        end
        function class:GetButtonPlusTips() return "新增一页局外仓库（免费）" end
    elseif module=="BP_InventoryCharacterSection" then
        local original=class.SetupWithContext
        local destruct=class.Destruct
        function class:SetupWithContext(context,mithril,gamepad)
            local result=original(self,context,mithril,gamepad)
            trace("offline backpack paging",function() setup_character(self,context) end)
            return result
        end
        function class:Destruct()
            local slots=self.WBP_TgfGame_InventorySlots
            if slots then open_grids[slots]=nil end
            return destruct(self)
        end
        function class:OnDragOver(geometry,event,operation)
            if operation and operation.DraggedItem then
                local absolute=UE.UKismetInputLibrary.PointerEvent_GetScreenSpacePosition(event)
                return M.drag_page(self,absolute,operation)
            end
            return false
        end
        function class:OnDragLeave(event,operation)
            local view=self.OfflineBackpackView
            if view then view.drag_operation,view.drag_direction=nil,nil end
        end
    end
    return class
end

return M
