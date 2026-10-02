local M = {}

local function clamp(x)
  return math.max(0, x)
end

function M.area(r)
  return math.pi * clamp(r) ^ 2
end

function M:describe()
  return tostring(self.r)
end

M.scale = function(r, k)
  return clamp(r) * k
end

local handlers = {
  on_draw = function(shape)
    return M.area(shape.r)
  end,
}

print(M.area(2))

return M
