-- The editor profile has reports and quickfix, but never permission or completion gates.
local root = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(root)
package.path = root .. '/tests/?.lua;' .. package.path
local overwatch = require('overwatch')
local fake = require('fake_editor')
local traces = vim.json.decode(table.concat(vim.fn.readfile(root .. '/tests/fixtures/integration-contract.json'), '\n'))
fake.install()
local flush = fake.flush
local function report(snapshot)
  local children = fake.children
  if children.cargo and children.cargo.ended then overwatch.changed(); flush() end
  local primary = #snapshot.primary > 0
  local secondary = #snapshot.secondary > 0
  children.zig.output(nil, primary and "error: 'a' failed:\nBuild Summary: 2/3 steps succeeded (1 failed)\n" or 'Build Summary: 3/3 steps succeeded\n')
  children.cargo.output(nil, secondary and 'test a ... FAILED\n' or 'test result: ok\n')
  children.cargo.ended = true
  children.cargo.exit({ code = secondary and 1 or 0 })
  flush()
end
--- Checks that the status line shows each watcher failing exactly when the contract expects it to.
local function check_report(trace, expected)
  local status = overwatch.status()
  for _, mapping in ipairs({ { 'primary', 'zig' }, { 'secondary', 'rust' } }) do
    local part = status:match('overwatch ' .. mapping[2] .. ' ([^·]+)')
    assert(part, trace.name .. ': missing ' .. mapping[2] .. ' in ' .. status)
    local expected_failed = vim.tbl_contains(expected.failures, mapping[1])
    assert((part:find('✗', 1, true) ~= nil) == expected_failed, trace.name .. ': ' .. status)
  end
end

--- Stops the editor, then checks that output arriving late from the stopped watcher changes nothing.
local function check_stop()
  local late = fake.children.zig
  overwatch.stop()
  vim.fn.setqflist({}, 'r', { title = 'user list', items = { { text = 'keep this' } } })
  local count = #fake.notices
  late.output(nil, "error: 'stale' failed:\nBuild Summary: 2/3 steps succeeded (1 failed)\n")
  flush()
  assert(overwatch.status() == '', 'stopped editor retained status')
  assert(vim.fn.getqflist({ title = 1 }).title == 'user list', 'stale callback overwrote user quickfix')
  assert(#fake.notices == count, 'stale callback notified after stop')
end

-- What each event of a contract trace does to the editor, given the replay's state and what the trace expects next.
local EVENTS = {
  RequestInspect = function(replay)
    local first = vim.fn.readfile(replay.directory .. '/build.zig')[1]
    assert(first, ('%s: build.zig in %s has no first line to inspect'):format(replay.trace.name, replay.directory))
    replay.response = 'allow'
  end,
  RequestRepair = function(replay)
    vim.fn.writefile({ 'repaired' }, replay.directory .. '/build.zig')
    replay.response = 'allow'
  end,
  RequestPublish = function(replay)
    vim.cmd('let g:overwatch_contract_action = 1')
    replay.response = 'allow'
  end,
  Respond = function(replay, expected) assert(replay.response == expected.decision, replay.trace.name .. ': ' .. replay.response) end,
  -- An editor cannot tell a person's save from another process's, so either finish is read the same way.
  Finish = function(replay)
    replay.failed = replay.trace.next
    report(replay.failed)
  end,
  Report = function(replay, expected)
    if replay.stopped then return end
    report(replay.failed or replay.trace.reports)
    check_report(replay.trace, expected)
  end,
  Stop = function(replay)
    replay.stopped = true
    check_stop()
  end,
  Restart = function(replay)
    replay.stopped = false
    overwatch.start(replay.directory)
    report(replay.trace.reports)
  end,
}
EVENTS.RequestComplete, EVENTS.Poll, EVENTS.OutsideFinish = EVENTS.RequestPublish, EVENTS.Respond, EVENTS.Finish

local function replay_contract(trace)
  fake.reset()
  local directory = vim.fn.tempname()
  vim.fn.mkdir(directory, 'p')
  vim.fn.writefile({ 'initial' }, directory .. '/build.zig')
  vim.fn.writefile({}, directory .. '/Cargo.toml')
  overwatch.start(directory)
  report(trace.baseline)
  report(trace.reports)
  local replay = { trace = trace, directory = directory, stopped = false, response = 'none', failed = nil }
  local ok, err = pcall(function()
    for index, event in ipairs(trace.events) do
      local expected = trace.expected[index + 1]
      if EVENTS[event] then EVENTS[event](replay, expected) end
      assert(expected.decision ~= 'deny' and expected.decision ~= 'block', 'editor contract must not invent an agent gate')
    end
  end)
  overwatch.stop(); vim.fn.delete(directory, 'rf')
  assert(ok, err)
end
local failures, count = 0, 0
for _, trace in ipairs(traces) do
  if trace.host == 'neovim' then
    count = count + 1
    local ok, err = pcall(replay_contract, trace)
    if not ok then failures = failures + 1; print('FAIL ' .. trace.name .. ': ' .. tostring(err)) end
  end
end
fake.restore()
print(('%d editor contracts, %d failures'):format(count, failures))
assert(failures == 0, ('%d of %d editor contracts failed'):format(failures, count))
