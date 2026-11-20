-- overwatch for Neovim: runs the project's test watchers beside the editor, one for each language it
-- has, shows the latest results in the status line and loads each failing build into the quickfix list.
local M = {}

local discovery = require('overwatch.discovery')
local reading = require('overwatch.reading')

M.OVERRIDE_PATH = discovery.OVERRIDE_PATH
M.WATCHERS = discovery.WATCHERS
M.find_watchers = discovery.find_watchers
M.vim_regex = reading.vim_regex
M.last_build = reading.last_build
M.read_report = reading.read_report
M.status_line = reading.status_line

local QUIET_MS = 400

-- A watcher that ends within this long of its start counts as a quick exit and waits before it restarts: this long
-- after the first, doubling after each further one. After GIVE_UP_AFTER in a row, only :OverwatchStart restarts it.
M.RETRY_MS = 30000
local GIVE_UP_AFTER = 3

-- Each run is one watcher at work: its process, its part of the status line, and its rerun after saves.
local state = { cwd = nil, runs = {} }

--- The latest results, for a status line component.
function M.status()
  local parts = {}
  for _, run in ipairs(state.runs) do
    if run.status ~= '' then parts[#parts + 1] = run.status end
  end
  assert(#parts <= #state.runs, ('%d status parts for %d runs'):format(#parts, #state.runs))
  local line = table.concat(parts, ' · ')
  assert(#parts == 0 or vim.startswith(line, 'overwatch '), ('the status line %q does not start with overwatch'):format(line))
  return line
end

--- Shows text as the run's part of the status line.
local function set_status(run, text)
  assert(vim.startswith(text, 'overwatch '), ('%q is not an overwatch status; start it with overwatch and the watcher\'s name'):format(text))
  run.status = text
  assert(run.status ~= '', ('%s was given an empty status'):format(run.watcher.name))
  vim.cmd.redrawstatus()
end

--- Whether run is still one start made and stop has not ended.
local function is_running(run)
  assert(type(run.index) == 'number' and run.index >= 1, ('a run has index %s; start numbers runs from 1'):format(tostring(run.index)))
  assert(run.watcher ~= nil, ('run %d has no watcher'):format(run.index))
  return state.runs[run.index] == run
end

--- Loads every failing watcher's report into one quickfix list, so two languages failing at once do not overwrite
--- each other's. A watcher back to green leaves the list, which is only replaced while it is still overwatch's.
local function update_quickfix(is_failed)
  assert(type(is_failed) == 'boolean', ('update_quickfix was given is_failed %s'):format(vim.inspect(is_failed)))
  local lines, names = {}, {}
  for _, run in ipairs(state.runs) do
    if run.failing_lines then
      vim.list_extend(lines, run.failing_lines)
      names[#names + 1] = run.watcher.name
    end
  end
  assert(not is_failed or #names > 0, ('is_failed is %s but none of the %d runs holds failing lines (names: %s); deliver sets them first'):format(tostring(is_failed), #state.runs, vim.inspect(names)))
  local title = vim.fn.getqflist({ title = 1 }).title
  if not is_failed and not vim.startswith(title, 'overwatch') then return end
  vim.fn.setqflist({}, 'r', { title = 'overwatch ' .. table.concat(names, ', '), lines = lines, efm = vim.o.errorformat })
end

--- Shows a run's report: its status, the quickfix list while it fails, and a notice when it fails or recovers.
local function deliver(run, report)
  local watcher = run.watcher
  set_status(run, M.status_line(watcher, report))
  local is_recovery = not report.is_failed and run.was_failing
  run.was_failing = report.is_failed
  run.failing_lines = report.is_failed and vim.split(report.text, '\n', { plain = true }) or nil
  assert(run.was_failing == report.is_failed, ('%s recorded was_failing %s for a report with is_failed %s'):format(watcher.name, tostring(run.was_failing), tostring(report.is_failed)))
  assert((run.failing_lines ~= nil) == report.is_failed, ('%s keeps failing lines only while it fails'):format(watcher.name))
  if report.is_failed or is_recovery then update_quickfix(report.is_failed) end
  if report.is_failed then
    local failing = #report.failures > 0 and (' (%s)'):format(table.concat(vim.list_slice(report.failures, 1, 3), ', ')) or ''
    vim.notify(('overwatch %s: %s%s; :copen for the report'):format(watcher.name, report.summary, failing), vim.log.levels.ERROR)
  elseif is_recovery then
    vim.notify(('overwatch %s: green again. %s'):format(watcher.name, report.summary), vim.log.levels.INFO)
  end
end

--- The command argv runs, as a person would type it.
local function command_line(argv)
  assert(type(argv) == 'table' and #argv > 0, ('command_line was given %s; pass a watcher\'s argv or baseline'):format(vim.inspect(argv)))
  local line = table.concat(argv, ' ')
  assert(vim.startswith(line, argv[1]), ('%q does not start with its program %s'):format(line, argv[1]))
  return line
end

--- Tells the user a command gave no result, with the last lines it printed.
local function report_broken(run, argv, why, output)
  assert(type(output) == 'string', ('report_broken was given %s output for %s'):format(type(output), run.watcher.name))
  local lines = vim.split(vim.trim(output), '\n', { plain = true })
  local shown = vim.list_slice(lines, math.max(1, #lines - 9))
  assert(#shown >= 1 and #shown <= 10, ('report_broken would show %d lines of %s; it shows the last one to ten'):format(#shown, run.watcher.name))
  local tail = table.concat(shown, '\n')
  set_status(run, ('overwatch %s: %s %s'):format(run.watcher.name, command_line(argv), why))
  vim.notify(('overwatch: %s %s\n%s'):format(command_line(argv), why, tail), vim.log.levels.ERROR)
end

--- Starts argv, handing each chunk of output to on_output on the main loop.
local function spawn(run, argv, on_output, on_exit)
  assert(state.cwd ~= nil, ('%s was spawned before start set the project directory'):format(run.watcher.name))
  local function read(err, data)
    assert(err == nil, ('reading what %s printed failed: %s'):format(command_line(argv), tostring(err)))
    assert(data == nil or type(data) == 'string', ('%s printed a %s, not text'):format(command_line(argv), type(data)))
    if data then vim.schedule(function() on_output(data) end) end
  end
  local cwd = run.watcher.dir and (state.cwd .. '/' .. run.watcher.dir) or state.cwd
  local ok, process = pcall(vim.system, argv, { cwd = cwd, env = run.watcher.env, stdout = read, stderr = read }, function(result)
    vim.schedule(function() on_exit(result) end)
  end)
  if not ok then
    set_status(run, ('overwatch %s: could not start %s'):format(run.watcher.name, command_line(argv)))
    return nil
  end
  assert(process ~= nil, ('vim.system started %s but gave no process'):format(command_line(argv)))
  run.process = process
  return process
end

--- Runs a command to its end and reports what it printed, unless it was stopped for a newer run first.
local function run_once(run, argv, done, is_stale)
  assert(type(done) == 'function', ('run_once for %s was given no done callback'):format(run.watcher.name))
  local output = ''
  local process = spawn(run, argv, function(data) output = output .. data end, function(result)
    assert(type(result.code) == 'number', ('%s ended with exit code %s'):format(command_line(argv), tostring(result.code)))
    run.process = nil
    if not is_running(run) then return end
    if not (is_stale and is_stale()) then
      local report = M.read_report(run.watcher, output, result.code)
      if report then
        deliver(run, report)
      else
        report_broken(run, argv, 'gave no summary', output)
      end
    end
    done()
  end)
  if process == nil then done() end
end

--- Reruns the suite once saves go quiet, one run at a time. A save made while it runs stops that run, whose result
--- would already be out of date, and starts another.
local function rerun_on_save(run)
  assert(run.rerun == nil, ('%s already reruns on save; begin it once for each start'):format(run.watcher.name))
  local timer, is_busy, is_pending = vim.uv.new_timer(), false, false
  local function rerun()
    if not is_running(run) then return end
    if is_busy then
      assert(run.process ~= nil, ('%s is busy with no process to stop; run_once clears busy when its run ends'):format(run.watcher.name))
      is_pending = true
      run.process:kill(15)
      return
    end
    is_busy, is_pending = true, false
    run_once(run, run.watcher.baseline, function()
      is_busy = false
      if is_pending then run.rerun(0) end
    end, function() return is_pending end)
    assert(not is_pending, ('a save to %s was pending before its run had begun'):format(run.watcher.name))
  end
  -- After delay ms with no further call: QUIET_MS after a save, at once after a run a save stopped.
  run.rerun = function(delay)
    assert(type(delay) == 'number' and delay >= 0, ('%s was asked to rerun after %s ms'):format(run.watcher.name, tostring(delay)))
    assert(is_running(run), ('%s was asked to rerun after stop ended it'):format(run.watcher.name))
    timer:start(delay, 0, vim.schedule_wrap(rerun))
  end
  assert(type(run.rerun) == 'function', ('%s registered no rerun'):format(run.watcher.name))
end

--- After a watcher ends of itself: one that ran RETRY_MS or longer restarts at once; a quick exit waits RETRY_MS,
--- doubling with each one in a row, until GIVE_UP_AFTER of them, when it is left stopped with what it last printed.
local function restart(run, started_at, what, output)
  assert(type(run.begin_again) == 'function', ('%s cannot restart: start gives each run its begin_again'):format(run.watcher.name))
  local is_quick = vim.uv.now() - started_at < M.RETRY_MS
  run.quick_exits = is_quick and run.quick_exits + 1 or 0
  if run.quick_exits >= GIVE_UP_AFTER then
    report_broken(run, run.watcher.argv, ('%s %d times in a row; :OverwatchStart to retry'):format(what, run.quick_exits), output)
    return
  end
  local delay = is_quick and M.RETRY_MS * 2 ^ (run.quick_exits - 1) or 0
  assert(delay >= 0 and delay <= M.RETRY_MS * 2 ^ (GIVE_UP_AFTER - 2), ('%s would wait %d ms to restart, past its longest backoff'):format(run.watcher.name, delay))
  local when = delay == 0 and 'now' or ('in %ds'):format(delay / 1000)
  set_status(run, ('overwatch %s: %s, restarting %s'):format(run.watcher.name, what, when))
  vim.notify(('overwatch: %s %s, restarting %s'):format(command_line(run.watcher.argv), what, when), vim.log.levels.WARN)
  vim.defer_fn(function()
    if is_running(run) then run.begin_again(run) end
  end, delay)
end

--- Delivers the latest complete build in what the watch command has printed, keeping what no summary ends yet.
local function flush_builds(run, printed)
  if not is_running(run) then return end
  local before = #printed.output
  local lines = printed.output:match('^(.*\n)') or ''
  local report = M.read_report(run.watcher, lines)
  if report == nil then return end
  local _, remaining = M.last_build(run.watcher, lines)
  assert(#remaining <= #lines, ('%s left %d bytes unread of the %d it read'):format(run.watcher.name, #remaining, #lines))
  printed.output = remaining .. printed.output:sub(#lines + 1)
  assert(#printed.output <= before, ('flushing %s grew its output from %d to %d bytes'):format(run.watcher.name, before, #printed.output))
  deliver(run, report)
end

--- Runs the watcher's watch command, delivering each build once its output goes quiet, and restarts it if it ends.
local function watch(run)
  local watcher = run.watcher
  assert(type(watcher.argv) == 'table' and #watcher.argv > 0, ('%s has no watch command to run'):format(watcher.name))
  local printed, recent, flush = { output = '' }, '', vim.uv.new_timer()
  local started_at = vim.uv.now()
  set_status(run, ('overwatch %s: starting %s'):format(watcher.name, command_line(watcher.argv)))
  local process = spawn(run, watcher.argv, function(data)
    if not is_running(run) then return end
    printed.output, recent = printed.output .. data, (recent .. data):sub(-4000)
    flush:start(QUIET_MS, 0, vim.schedule_wrap(function() flush_builds(run, printed) end))
  end, function(result)
    flush:stop()
    if not is_running(run) then return end
    run.process = nil
    local report = M.read_report(watcher, printed.output)
    if report then deliver(run, report) end
    restart(run, started_at, ('exited with code %d'):format(result.code), recent)
  end)
  assert(process == nil or run.process == process, ('%s started a process spawn did not record'):format(watcher.name))
  if process == nil then restart(run, started_at, 'could not start', '') end
end

--- Once the full suite has run: the watch command, or the suite again after each save.
local function keep_watching(run)
  local watcher = run.watcher
  if not is_running(run) then return end
  assert(watcher.argv or watcher.baseline, ('%s has neither a watch command nor a suite to rerun'):format(watcher.name))
  if watcher.argv then
    watch(run)
  elseif watcher.baseline then
    rerun_on_save(run)
  end
  assert(watcher.argv or run.rerun ~= nil, ('%s reruns its suite on save but never registered how'):format(watcher.name))
end

--- Runs one watcher: its full suite first, then its watch command, or its suite again after each save.
local function begin(run)
  local watcher = run.watcher
  assert(is_running(run), ('%s began after stop ended it'):format(watcher.name))
  assert(run.process == nil, ('%s began while its last process still runs'):format(watcher.name))
  if watcher.baseline and #watcher.baseline > 0 then
    set_status(run, ('overwatch %s: running the full suite with %s'):format(watcher.name, command_line(watcher.baseline)))
    run_once(run, watcher.baseline, function() keep_watching(run) end)
  else
    keep_watching(run)
  end
end

--- Starts watching cwd (default: the current directory); does nothing when it has no watcher.
function M.start(cwd)
  M.stop()
  state.cwd = cwd or vim.fn.getcwd()
  local found = M.find_watchers(state.cwd)
  for index, watcher in ipairs(found) do
    state.runs[index] = { index = index, watcher = watcher, status = '', was_failing = false, quick_exits = 0, begin_again = begin }
  end
  assert(#state.runs == #found, ('%d runs for the %d watchers of %s'):format(#state.runs, #found, state.cwd))
  for _, run in ipairs(state.runs) do
    begin(run)
  end
  assert(vim.iter(state.runs):all(function(run) return run.status ~= '' end), ('a watcher of %s began without showing a status'):format(state.cwd))
end

--- Stops every watcher and clears its part of the status line.
function M.stop()
  local runs = state.runs
  state.runs = {}
  for _, run in ipairs(runs) do
    if run.process then run.process:kill(15) end
  end
  assert(vim.tbl_isempty(state.runs), ('stop left %d runs'):format(#state.runs))
  assert(M.status() == '', ('stop left the status %q'):format(M.status()))
  vim.cmd.redrawstatus()
end

--- Called after a buffer is written; reruns each watcher that has no watch command of its own.
function M.changed()
  local reruns = 0
  for _, run in ipairs(state.runs) do
    if run.rerun then
      run.rerun(QUIET_MS)
      reruns = reruns + 1
    end
  end
  assert(reruns <= #state.runs, ('%d reruns for %d runs'):format(reruns, #state.runs))
  assert(reruns == 0 or state.cwd ~= nil, 'a watcher reruns with no project directory')
end

return M
