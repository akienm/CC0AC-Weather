-- CC0AC-Weather on a jailbroken Kindle: fetch the station's current.json and forecast.js, draw them
-- with FBInk, sleep, repeat. The station does no extra work: these are files it already writes.
-- Run: lua weather.lua http://STATION:12345 [seconds]   (needs curl, FBInk, Lua + cjson; all on a KT3)
local cjson = require("cjson")
local STATION = arg[1] or "http://127.0.0.1:12345"
local EVERY = tonumber(arg[2] or "300")
local FBINK = os.getenv("FBINK") or "/mnt/us/libkh/bin/fbink"
local F = "/usr/java/lib/fonts/"
local REG, BOLD = F .. "Amazon-Ember-Regular.ttf", F .. "Amazon-Ember-Bold.ttf"
local W, H = 600, 800
local STOP = "/mnt/us/cc0ac-weather/stop"
local RETRY = "/mnt/us/cc0ac-weather/retry"          -- run.sh leaves it when the Try now button is touched
local RETRY_BOX = "/mnt/us/cc0ac-weather/retry-box"  -- where that button is, while it shows
local BOX = {60, 500, 540, 600}                      -- left, top, right, bottom

local function q(s) return "'" .. tostring(s):gsub("'", "'\\''") .. "'" end

local function fetch(path)
  local p = io.popen("curl -s -m 20 " .. q(STATION .. path))
  local body = p:read("*a"); p:close()
  return body ~= "" and body or nil
end

local function decode(body)
  if not body then return nil end
  body = body:gsub("^%s*window%.[%w_]+%s*=%s*", ""):gsub(";%s*$", "")
  local ok, v = pcall(cjson.decode, body)
  return ok and v or nil
end

local cmds = {}
-- text(s, x, y, px, bold, right): one line of TrueType text px pixels tall, top-left at x,y, clipped
-- to one line and to the column ending at `right` (default: the screen's right margin)
local function text(s, x, y, px, bold, right)
  local font = bold and BOLD or REG
  local bottom = H - (y + math.floor(px * 1.35))
  cmds[#cmds + 1] = string.format("%s -q -b -O -t regular=%s,px=%d,top=%d,bottom=%d,left=%d,right=%d %s",
    FBINK, font, px, y, bottom, x, W - (right or W - 16), q(s))
end
local function rule(y) cmds[#cmds + 1] = string.format("%s -q -b -k top=%d,left=20,width=%d,height=2 -B BLACK", FBINK, y, W - 40) end
local function draw(full)
  table.insert(cmds, 1, FBINK .. " -q -b -k")             -- clear, no refresh yet
  cmds[#cmds + 1] = FBINK .. " -q -s" .. (full and " -f" or "")   -- one refresh for the whole screen
  os.execute(table.concat(cmds, " ; ") .. " >/dev/null 2>&1")
  cmds = {}
end

local function num(v, fmt) return v and string.format(fmt or "%.0f", v) or "--" end
local DIRS = {"N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"}
local function compass(deg) return deg and DIRS[math.floor((deg % 360) / 22.5 + 0.5) % 16 + 1] or "" end

local function iso_utc(s)   -- "2026-11-20T17:45:14Z" or "...-07:00" -> epoch
  local y, mo, d, h, mi, se, tz = s:match("(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)(.*)")
  if not y then return nil end
  local t = os.time({year = y, month = mo, day = d, hour = h, min = mi, sec = se})
  local utc_t = os.time(os.date("!*t", t))
  t = t + (t - utc_t)                                    -- os.time read it as local; undo that
  local sign, oh, om = tz:match("([+-])(%d%d):(%d%d)")
  if sign then t = t - (sign == "+" and 1 or -1) * (oh * 3600 + om * 60) end
  return t
end
local function offset_of(s) local sign, oh, om = (s or ""):match("([+-])(%d%d):(%d%d)$")
  return sign and (sign == "+" and 1 or -1) * (oh * 3600 + om * 60) or 0 end

local function screen(cur, fc, n)
  local off = offset_of(fc and fc.daily and fc.daily.periods and fc.daily.periods[1] and fc.daily.periods[1].start)
  local now = os.time()
  local function hm(t) return os.date("!%H:%M", t + off) end
  local out, rooms = nil, {}
  for _, s in ipairs(cur.sensors or {}) do
    if s.type == "Atlas" or s.type == "5-in-1" or s.type == "5n1" then out = out or s else rooms[#rooms + 1] = s end
  end
  local f = out and out.fields or {}
  local T = 44                                           -- the Kindle's own status bar sits above this
  text((cur.title or "Weather") .. "   " .. os.date("!%a %b %d  %H:%M", now + off), 24, T, 26, true)
  rule(T + 40)
  -- now: the big number, details beside it
  text(num(f.temp_f) .. "°", 16, T + 46, 150, true, 300)
  local y = T + 56
  local trend = cur.pressure_change_3h
  trend = trend and (trend > 0.02 and " rising" or trend < -0.02 and " falling" or " steady") or ""
  for _, line in ipairs({
      "Feels " .. num(f.feels_like_f) .. "°  Dew " .. num(f.dew_point_f) .. "°",
      "Humidity " .. num(f.humidity_pct) .. "%",
      "Wind " .. num(f.wind_mph) .. " " .. compass(f.wind_dir_deg) .. "  gust " .. num(f.wind_gust_mph),
      num(f.pressure_inhg, "%.2f") .. " in" .. trend,
      "Rain " .. num(f.rain_day_in, "%.2f") .. " in  UV " .. num(f.uv_index) }) do
    text(line, 306, y, 25, false); y = y + 38
  end
  text(cur.zambretti or "", 24, T + 252, 30, true)
  rule(T + 300)
  -- forecast: the next four periods
  y = T + 314
  local periods = fc and fc.daily and fc.daily.periods or {}
  for i = 1, math.min(4, #periods) do
    local p = periods[i]
    text(p.name or "", 24, y, 27, true, 236)
    text((p.day and "High " or "Low ") .. num(p.temp_f) .. "°", 240, y, 27, false, 380)
    text((p.sky or "") .. ((p.pop or 0) >= 20 and ("  " .. p.pop .. "%") or ""), 384, y, 22, false)
    y = y + 42
  end
  rule(y + 4)
  -- rooms, two columns
  y = y + 18
  table.sort(rooms, function(a, b) return (a.name or "") < (b.name or "") end)
  for i, s in ipairs(rooms) do
    local x = (i % 2 == 1) and 24 or 308
    text(s.name or s.sensor_id, x, y, 23, false, x + 160)
    text(num(s.fields and s.fields.temp_f) .. "° " .. num(s.fields and s.fields.humidity_pct) .. "%", x + 164, y, 23, true, x + 284)
    if i % 2 == 0 then y = y + 36 end
  end
  if #rooms % 2 == 1 then y = y + 36 end
  -- warnings and alerts
  local warn = {}
  for _, w in ipairs(cur.warnings or {}) do warn[#warn + 1] = type(w) == "table" and (w.text or w.title or w.message) or tostring(w) end
  for _, a in ipairs(fc and fc.alerts or {}) do warn[#warn + 1] = a.event or a.headline or a.title end
  for i = 1, math.min(3, #warn) do text("! " .. warn[i], 24, y + 8, 24, true); y = y + 36 end
  -- footer: how fresh the readings are
  local w = cur.written and iso_utc(cur.written)
  local age = w and (now - w) or nil
  text("Readings " .. (w and hm(w) or "?") ..
       (age and age > 900 and ("  (" .. math.floor(age / 60) .. " min old!)") or "") .. "   Touch the screen to leave",
       24, H - 34, 18, age and age > 900)
  draw(n % 6 == 0)          -- a black flash every 6th refresh clears e-ink ghosting
end

local function exists(path) local f = io.open(path); if f then f:close() end; return f ~= nil end

local function box(l, t, r, b, w)                       -- an outline w pixels thick
  for _, k in ipairs({{l, t, r - l, w}, {l, b - w, r - l, w}, {l, t, w, b - t}, {r - w, t, w, b - t}}) do
    cmds[#cmds + 1] = string.format("%s -q -b -k top=%d,left=%d,width=%d,height=%d -B BLACK", FBINK, k[2], k[1], k[3], k[4])
  end
end

local function failed(why, n)
  text("Station not answering", 24, 330, 34, true)
  text(why, 24, 384, 22, false)
  text("Trying again every " .. EVERY .. " s", 24, 424, 22, false)
  local l, t, r, b = BOX[1], BOX[2], BOX[3], BOX[4]
  box(l, t, r, b, 4)
  text("Try now", l + 150, t + 30, 34, true, r - 8)
  text("Touch anywhere else to leave", 24, b + 30, 22, false)
  draw(n % 6 == 0)
  local f = io.open(RETRY_BOX, "w"); if f then f:write(table.concat(BOX, " "), "\n"); f:close() end
end

-- After a drop the Kindle rejoins Wi-Fi only when its own interface wants the network (a book, the
-- store), never for a script, so a dropped connection stayed down until someone left the screen and
-- opened something (measured 2026-11-21: 75 minutes). Asked directly, it is back in about 5 s.
local function wifi_up()
  local p = io.popen("wpa_cli -i wlan0 status 2>/dev/null"); local s = p:read("*a"); p:close()
  return s:find("wpa_state=COMPLETED", 1, true) ~= nil
end
local function rejoin()
  if wifi_up() then return false end
  io.stderr:write(os.date() .. " Wi-Fi down; asking it to reconnect\n")
  os.execute("wpa_cli -i wlan0 reconnect >/dev/null 2>&1")
  for _ = 1, 45 do
    if wifi_up() then os.execute("sleep 3"); io.stderr:write(os.date() .. " Wi-Fi back\n"); return true end
    os.execute("sleep 1")
  end
  io.stderr:write(os.date() .. " Wi-Fi still down\n")
  return false
end

local function trying()                                 -- the button was touched: say so before the wait
  text("Trying now...", BOX[1] + 4, BOX[4] + 70, 26, true)
  cmds[#cmds + 1] = FBINK .. " -q -s"
  os.execute(table.concat(cmds, " ; ") .. " >/dev/null 2>&1")
  cmds = {}
end

os.execute("lipc-set-prop com.lab126.powerd preventScreenSaver 1")
local n = 0
while true do
  local f = io.open(STOP); if f then f:close(); os.remove(STOP); break end
  local cur = decode(fetch("/current.json"))
  if not cur and rejoin() then cur = decode(fetch("/current.json")) end
  local ok, err
  if cur then ok, err = pcall(screen, cur, decode(fetch("/forecast.js")), n)
  else ok, err = false, "no answer from " .. STATION .. (wifi_up() and "" or " (Wi-Fi is down)") end
  if ok then os.remove(RETRY_BOX)
  else cmds = {}; failed(tostring(err), n); io.stderr:write(os.date() .. " " .. tostring(err) .. "\n") end
  n = n + 1
  os.remove(RETRY)
  for _ = 1, EVERY do                                   -- a second at a time, so a Try now is seen at once
    if exists(RETRY) or exists(STOP) then break end
    os.execute("sleep 1")
  end
  if exists(RETRY) then trying() end
end
os.remove(RETRY_BOX)
os.execute("lipc-set-prop com.lab126.powerd preventScreenSaver 0")
