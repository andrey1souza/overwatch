-- The editor profile has reports and quickfix, but never permission or completion gates.
local root = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(root)
local overwatch = require('overwatch')
local traces = vim.json.decode(table.concat(vim.fn.readfile(root .. '/tests/fixtures/integration-contract.json'), '\n'))
local saved = { system = vim.system, timer = vim.uv.new_timer, schedule = vim.schedule, wrap = vim.schedule_wrap, notify = vim.notify }
local children, timers, notices = {}, {}, {}
vim.schedule = function(fn) fn() end
vim.schedule_wrap = function(fn) return fn end
vim.notify = function(text) notices[#notices + 1] = text end
vim.system = function(argv, options, exit)
  children[argv[1]] = { output = options.stdout, exit = exit, ended = false }
  return { kill = function() end }
end
vim.uv.new_timer = function()
  local timer = { start = function(self, _, _, fn) self.callback = fn end, stop = function(self) self.callback = nil end }
  timers[#timers + 1] = timer
  return timer
end
local function flush()
  for _ = 1, 20 do
    local any = false
    for _, timer in ipairs(timers) do
      if timer.callback then local fn = timer.callback; timer.callback = nil; fn(); any = true end
    end
    if not any then return end
  end
  error('editor callbacks did not quiesce')
end
local function report(snapshot)
  if children.cargo and children.cargo.ended then overwatch.changed(); flush() end
  local primary = #snapshot.primary > 0
  local secondary = #snapshot.secondary > 0
  children.zig.output(nil, primary and "error: 'a' failed:\nBuild Summary: 2/3 steps succeeded (1 failed)\n" or 'Build Summary: 3/3 steps succeeded\n')
  children.cargo.output(nil, secondary and 'test a ... FAILED\n' or 'test result: ok\n')
  children.cargo.ended = true
  children.cargo.exit({ code = secondary and 1 or 0 })
  flush()
end
local function replay(trace)
  children, timers, notices = {}, {}, {}
  local directory = vim.fn.tempname()
  vim.fn.mkdir(directory, 'p')
  vim.fn.writefile({ 'initial' }, directory .. '/build.zig')
  vim.fn.writefile({}, directory .. '/Cargo.toml')
  local function start() overwatch.start(directory); report(trace.reports) end
  overwatch.start(directory)
  report(trace.baseline)
  report(trace.reports)
  local stopped, response, failed = false, 'none', nil
  local ok, err = pcall(function()
    for index, event in ipairs(trace.events) do
      local expected = trace.expected[index + 1]
      if event == 'RequestInspect' then assert(vim.fn.readfile(directory .. '/build.zig')[1]); response = 'allow' end
      if event == 'RequestRepair' then vim.fn.writefile({ 'repaired' }, directory .. '/build.zig'); response = 'allow' end
      if event == 'RequestPublish' or event == 'RequestComplete' then vim.cmd('let g:overwatch_contract_action = 1'); response = 'allow' end
      if event == 'Respond' or event == 'Poll' then assert(response == expected.decision, trace.name .. ': ' .. response) end
      -- An editor cannot tell a person's save from another process's, so either finish is read the same way.
      if event == 'Finish' or event == 'OutsideFinish' then failed = trace.next; report(failed) end
      if event == 'Report' and not stopped then
        report(failed or trace.reports)
        local status = overwatch.status()
        for _, mapping in ipairs({ { 'primary', 'zig' }, { 'secondary', 'rust' } }) do
          local part = status:match('overwatch ' .. mapping[2] .. ' ([^·]+)')
          assert(part, trace.name .. ': missing ' .. mapping[2] .. ' in ' .. status)
          local expected_failed = vim.tbl_contains(expected.failures, mapping[1])
          assert((part:find('✗', 1, true) ~= nil) == expected_failed, trace.name .. ': ' .. status)
        end
      end
      if event == 'Stop' then
        stopped = true
        local late = children.zig
        overwatch.stop()
        vim.fn.setqflist({}, 'r', { title = 'user list', items = { { text = 'keep this' } } })
        local count = #notices
        late.output(nil, "error: 'stale' failed:\nBuild Summary: 2/3 steps succeeded (1 failed)\n")
        flush()
        assert(overwatch.status() == '', 'stopped editor retained status')
        assert(vim.fn.getqflist({ title = 1 }).title == 'user list', 'stale callback overwrote user quickfix')
        assert(#notices == count, 'stale callback notified after stop')
      end
      if event == 'Restart' then stopped = false; start() end
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
    local ok, err = pcall(replay, trace)
    if not ok then failures = failures + 1; print('FAIL ' .. trace.name .. ': ' .. tostring(err)) end
  end
end
vim.system, vim.uv.new_timer, vim.schedule, vim.schedule_wrap, vim.notify = saved.system, saved.timer, saved.schedule, saved.wrap, saved.notify
print(('%d editor contracts, %d failures'):format(count, failures))
os.exit(failures == 0 and 0 or 1)
