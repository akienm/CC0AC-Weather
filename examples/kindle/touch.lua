-- touch.lua: wait for one tap on the touchscreen and print where it landed, "x y" in screen pixels,
-- once the finger lifts. Reads the kernel's input events (16 bytes each on the Kindle's 32-bit ARM).
-- Run: lua touch.lua [device] [x scale] [y scale]
local dev = io.open(arg[1] or "/dev/input/event0", "rb")
if not dev then io.stderr:write("touch.lua: can't open the touchscreen\n"); os.exit(1) end
local sx, sy = tonumber(arg[2] or "1"), tonumber(arg[3] or "1")
local function u(s, i, n) local v = 0; for k = n, 1, -1 do v = v * 256 + s:byte(i + k - 1) end; return v end
local x, y, down = nil, nil, false
while true do
  local e = dev:read(16)
  if not e or #e < 16 then os.exit(1) end
  local typ, code, val = u(e, 9, 2), u(e, 11, 2), u(e, 13, 4)
  if val >= 2147483648 then val = val - 4294967296 end
  if typ == 3 and (code == 0x35 or code == 0x00) then x, down = val, true        -- ABS_MT_POSITION_X / ABS_X
  elseif typ == 3 and (code == 0x36 or code == 0x01) then y, down = val, true    -- ABS_MT_POSITION_Y / ABS_Y
  elseif (typ == 3 and code == 0x39 and val == -1)                                -- ABS_MT_TRACKING_ID: lifted
      or (typ == 1 and code == 0x14a and val == 0) then                           -- BTN_TOUCH up
    if down and x and y then io.write(math.floor(x * sx + 0.5), " ", math.floor(y * sy + 0.5), "\n")
    else io.write("-1 -1\n") end                       -- a touch with no place still counts (as leave)
    os.exit(0)
  end
end
