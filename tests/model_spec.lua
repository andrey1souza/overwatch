-- Replay generated build/chunk events through the real Neovim watcher callbacks.
local root = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(root)
package.path = root .. '/tests/?.lua;' .. package.path
local overwatch = require('overwatch')
local fake = require('fake_editor')
local traces = vim.json.decode(table.concat(vim.fn.readfile(root .. '/tests/fixtures/model-traces.json'), '\n'))
fake.install()

--- What zig prints for one of the model's build events: the failures of the workspace's version, then the summary,
--- or the part of them that event stands for after the partial event before it.
local function build_text(names, event, partial)
  local body = ''
  for _, name in ipairs(names) do body = body .. "error: '" .. name .. "' failed:\n" end
  local summary = #names == 0 and 'Build Summary: 3/3 steps succeeded; 1/1 tests passed\n'
    or ('Build Summary: 2/3 steps succeeded (1 failed); 0/%d tests passed (%d failed)\n'):format(#names, #names)
  if event == 'Partial' then return body end
  if event == 'PartialSummary' then return body .. 'Build Summary:' end
  if partial == 'PartialSummary' then return summary:sub(#'Build Summary:' + 1) end
  return (partial == '' and body or '') .. summary
end

--- Checks the status line against the failures the model expects after event, when it knows them.
local function check_status(trace, event, expected)
  if not expected.known then return end
  local status = overwatch.status()
  local where = trace.name .. ': ' .. event .. ': ' .. status
  assert((status:find(' ✗ ', 1, true) ~= nil) == (#expected.failures > 0), where)
  assert(status:match(': ([ab])') == expected.failures[1], where)
  local more = status:match('%+(%d+) more$')
  assert((more and tonumber(more, 10) or 0) == math.max(0, #expected.failures - 1), where)
end

local function replay_model(trace)
  fake.reset()
  local directory = vim.fn.tempname()
  vim.fn.mkdir(directory, 'p')
  vim.fn.writefile({}, directory .. '/build.zig')
  local version, partial = 1, ''
  overwatch.start(directory)
  local ok, err = pcall(function()
    for index, event in ipairs(trace.events) do
      -- Another process's save is a new version too; the editor sees it like any other.
      if event == 'Edit' or event == 'Move' or event == 'Outside' then version = version + 1 end
      if event == 'Build' or event == 'Partial' or event == 'PartialSummary' then
        fake.children.zig.output(nil, build_text(trace.truth[version], event, partial))
        partial = event == 'Build' and '' or event
      end
      if event == 'Read' or event == 'Poll' then
        local timer = fake.timers[#fake.timers]
        assert(timer.callback, 'model read has no output callback')
        local callback = timer.callback
        timer.callback = nil
        callback()
      end
      check_status(trace, event, trace.expected[index + 1])
    end
  end)
  overwatch.stop()
  vim.fn.delete(directory, 'rf')
  assert(ok, err)
end

local failures = 0
for _, trace in ipairs(traces) do
  local ok, err = pcall(replay_model, trace)
  if not ok then failures = failures + 1; print('FAIL ' .. trace.name .. ': ' .. tostring(err)) end
end
fake.restore()
print(('%d Neovim model traces, %d failures'):format(#traces, failures))
assert(failures == 0, ('%d of %d Neovim model traces failed'):format(failures, #traces))
