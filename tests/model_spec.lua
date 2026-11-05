-- Replay generated build/chunk events through the real Neovim watcher callbacks.
local root = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(root)
local overwatch = require('overwatch')
local traces = vim.json.decode(table.concat(vim.fn.readfile(root .. '/tests/fixtures/model-traces.json'), '\n'))
local system, new_timer, schedule, schedule_wrap, notify = vim.system, vim.uv.new_timer, vim.schedule, vim.schedule_wrap, vim.notify
local output, timer
vim.schedule = function(fn) fn() end
vim.schedule_wrap = function(fn) return fn end
vim.notify = function() end
vim.system = function(_, options)
  output = options.stdout
  return { kill = function() end }
end
vim.uv.new_timer = function()
  timer = { start = function(self, _, _, fn) self.callback = fn end, stop = function(self) self.callback = nil end }
  return timer
end

local function replay(trace)
  local directory = vim.fn.tempname()
  vim.fn.mkdir(directory, 'p')
  vim.fn.writefile({}, directory .. '/build.zig')
  local version, partial = 1, ''
  overwatch.start(directory)
  local ok, err = pcall(function()
    for index, event in ipairs(trace.events) do
      if event == 'Edit' or event == 'Move' then version = version + 1 end
      if event == 'Build' or event == 'Partial' or event == 'PartialSummary' then
        local names = trace.truth[version]
        local body = ''
        for _, name in ipairs(names) do body = body .. "error: '" .. name .. "' failed:\n" end
        local summary = #names == 0 and 'Build Summary: 3/3 steps succeeded; 1/1 tests passed\n'
          or ('Build Summary: 2/3 steps succeeded (1 failed); 0/%d tests passed (%d failed)\n'):format(#names, #names)
        local text = event == 'Partial' and body or event == 'PartialSummary' and body .. 'Build Summary:'
          or partial == 'PartialSummary' and summary:sub(#'Build Summary:' + 1) or (partial == '' and body or '') .. summary
        output(nil, text)
        partial = event == 'Build' and '' or event
      end
      if event == 'Read' or event == 'Poll' then
        assert(timer.callback, 'model read has no output callback')
        local callback = timer.callback
        timer.callback = nil
        callback()
      end
      local expected, status = trace.expected[index + 1], overwatch.status()
      if expected.known then
        assert((status:find(' ✗ ', 1, true) ~= nil) == (#expected.failures > 0), trace.name .. ': ' .. event .. ': ' .. status)
        local first = status:match(': ([ab])')
        assert(first == expected.failures[1], trace.name .. ': ' .. event .. ': ' .. status)
        local extra = tonumber(status:match('%+(%d+) more$')) or 0
        assert(extra == math.max(0, #expected.failures - 1), trace.name .. ': ' .. event .. ': ' .. status)
      end
    end
  end)
  overwatch.stop()
  vim.fn.delete(directory, 'rf')
  assert(ok, err)
end

local failures = 0
for _, trace in ipairs(traces) do
  local ok, err = pcall(replay, trace)
  if not ok then failures = failures + 1; print('FAIL ' .. trace.name .. ': ' .. tostring(err)) end
end
vim.system, vim.uv.new_timer, vim.schedule, vim.schedule_wrap, vim.notify = system, new_timer, schedule, schedule_wrap, notify
print(('%d Neovim model traces, %d failures'):format(#traces, failures))
os.exit(failures == 0 and 0 or 1)
