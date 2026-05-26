-- Ring Fit Miners: all mining drills are .active=false unless storage.running
-- is true. The Python bridge (factorio_bridge.py) flips that flag over RCON by
-- invoking the /ring_fit_running command on edges of LegTracker's run/sprint
-- state. We only walk the surfaces on edges; new drills inherit the current
-- flag at build time.

local function set_all_drills(active)
  for _, surface in pairs(game.surfaces) do
    for _, drill in pairs(surface.find_entities_filtered{ type = "mining-drill" }) do
      drill.active = active
    end
  end
end

local function apply(running)
  running = running and true or false
  if storage.running == running then return end
  storage.running = running
  set_all_drills(running)
  game.print("[ring-fit] miners " .. (running and "ON (running)" or "OFF (idle)"))
end

script.on_init(function()
  storage.running = false
  set_all_drills(false)
end)

script.on_configuration_changed(function()
  if storage.running == nil then storage.running = false end
  set_all_drills(storage.running)
end)

local function on_built(event)
  local e = event.entity or event.created_entity
  if e and e.valid and e.type == "mining-drill" then
    e.active = storage.running and true or false
  end
end

script.on_event({
  defines.events.on_built_entity,
  defines.events.on_robot_built_entity,
  defines.events.on_entity_cloned,
  defines.events.script_raised_built,
  defines.events.script_raised_revive,
}, on_built)

remote.add_interface("ring_fit_miners", {
  set_running = function(running) apply(running) end,
  get_running = function() return storage.running end,
})

-- Custom command (vs /c) so achievements aren't disabled when the bridge pokes us.
commands.add_command(
  "ring_fit_running",
  "Set the Ring Fit running state. Usage: /ring_fit_running 1|0",
  function(cmd)
    local v = cmd.parameter
    apply(v == "1" or v == "true" or v == "on")
  end
)

commands.add_command(
  "ring_fit_status",
  "Show whether Ring Fit miners are currently enabled.",
  function()
    game.print("[ring-fit] running=" .. tostring(storage.running))
  end
)
