-- Dungeon item widgets also read the native definition's stack limit.
local M={}
local available,config=pcall(require,"OfflineProbe.PotionDefinitions")

function M.apply(mark)
    if not available then return end
    local updated,total=0,0
    for _,path in pairs(config.paths) do
        total=total+1
        local ok,err=pcall(function()
            local class=assert(UE.UClass.Load(path),"missing original consumable class")
            local definition=assert(UE.UDataUtility.GetClassDefaultObject(class),"missing original consumable definition")
            definition.MaxStackCount=config.limit
            assert(definition.MaxStackCount==config.limit,"original consumable cap did not update")
        end)
        if ok then updated=updated+1 elseif mark then mark("native consumable definition: " .. path .. " " .. tostring(err)) end
    end
    if mark then mark("original potion and bandage definition caps=" .. updated .. "/" .. total .. " limit=" .. config.limit) end
end

function M.cell(item)
    if not available or not item or not config.paths[item:GetTypeId()] then return end
    local definition=item:GetItemDefinitionObject(false)
    if definition and definition.MaxStackCount~=config.limit then definition.MaxStackCount=config.limit end
end

function M.context(context)
    if not available or not context then return end
    local kind=context.InventoryType
    if kind~=UE.ETgfInventoryType.InGameLocalPlayer and kind~=UE.ETgfInventoryType.InGameLoot then return end
    local items=context:GetInventoryItemsOfPage(false)
    for i=1,items:Num() do M.cell(items:Get(i)) end
end

return M
