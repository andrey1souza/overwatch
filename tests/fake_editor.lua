-- Stands in for the parts of Neovim that start processes, time builds and notify, so a spec can replay a trace
-- through overwatch's real callbacks one event at a time.
local M = { children = {}, timers = {}, notices = {} }

local real = { system = vim.system, new_timer = vim.uv.new_timer, schedule = vim.schedule, schedule_wrap = vim.schedule_wrap, notify = vim.notify }

--- Forgets the children, timers and notices of the last replay.
function M.reset()
  M.children, M.timers, M.notices = {}, {}, {}
  assert(vim.tbl_isempty(M.children) and vim.tbl_isempty(M.timers), ('reset left children %s and %d timers of the last replay'):format(vim.inspect(vim.tbl_keys(M.children)), #M.timers))
  assert(#M.notices == 0, ('reset left %d notices'):format(#M.notices))
end

--- Replaces process starts, timers, scheduling and notices with fakes: a child is kept by its program's name, a
--- timer keeps its callback until the spec runs it, and scheduled work runs at once.
function M.install()
  assert(vim.system == real.system, 'the fake editor is already installed; restore it first')
  vim.schedule = function(fn) fn() end
  vim.schedule_wrap = function(fn) return fn end
  vim.notify = function(text) M.notices[#M.notices + 1] = text end
  vim.system = function(argv, options, exit)
    M.children[argv[1]] = { output = options.stdout, exit = exit, ended = false }
    return { kill = function() end }
  end
  vim.uv.new_timer = function()
    local timer = { start = function(self, _, _, fn) self.callback = fn end, stop = function(self) self.callback = nil end }
    M.timers[#M.timers + 1] = timer
    return timer
  end
  assert(vim.system ~= real.system and vim.uv.new_timer ~= real.new_timer, 'the fake editor did not replace vim.system and vim.uv.new_timer')
end

--- Runs every timer's callback until none is left, as the editor would once each delay passed.
function M.flush()
  for _ = 1, 20 do
    local ran = 0
    for _, timer in ipairs(M.timers) do
      if timer.callback then
        local fn = timer.callback
        timer.callback = nil
        fn()
        ran = ran + 1
      end
    end
    if ran == 0 then return end
  end
  assert(false, ('editor callbacks did not quiesce after 20 rounds of %d timers'):format(#M.timers))
end

--- Puts the real process starts, timers, scheduling and notices back.
function M.restore()
  vim.system, vim.uv.new_timer, vim.schedule, vim.schedule_wrap, vim.notify = real.system, real.new_timer, real.schedule, real.schedule_wrap, real.notify
  assert(vim.system == real.system and vim.uv.new_timer == real.new_timer, 'restore left the fake vim.system or timer in place')
  assert(vim.notify == real.notify, 'restore left the fake vim.notify in place')
end

return M
