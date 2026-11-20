-- How overwatch reads what a watcher prints: its JavaScript patterns as Vim regexes, the latest complete build, the
-- report that build gives, and its part of the status line.
local M = {}

local LAZY = { ['*'] = '{-}', ['+'] = '{-1,}', ['?'] = '{-,1}' }

--- Copies the bracket expression that opens at js[i] into out up to its body: the [, a leading ^, and a ] that
--- stands for itself first in it. Returns the index after what it copied.
local function open_class(js, i, out)
  assert(js:sub(i, i) == '[', ('open_class was called at %q of %s, not at a ['):format(js:sub(i, i), js))
  local copied, after = #out, i + 1
  out[#out + 1] = '['
  if js:sub(after, after) == '^' then
    out[#out + 1] = '^'
    after = after + 1
  end
  if js:sub(after, after) == ']' then
    out[#out + 1] = ']'
    after = after + 1
  end
  assert(#out - copied == after - i, ('open_class copied %d characters of %s for %d it read'):format(#out - copied, js, after - i))
  return after
end

--- Translates a JavaScript regex into a very magic Vim regex.
function M.vim_regex(js)
  assert(type(js) == 'string', ('vim_regex was given a %s; pass a pattern from watchers.json or .overwatch.json'):format(type(js)))
  local out, i, in_class = { [[\v]] }, 1, false
  while i <= #js do
    local c, nxt = js:sub(i, i), js:sub(i + 1, i + 1)
    if c == '\\' then
      out[#out + 1] = (not in_class and nxt == 'b') and '%(<|>)' or c .. nxt
      i = i + 2
    elseif in_class then
      if c == ']' then in_class = false end
      out[#out + 1] = c
      i = i + 1
    elseif c == '[' then
      in_class = true
      i = open_class(js, i, out)
    elseif c == '(' and js:sub(i, i + 2) == '(?:' then
      out[#out + 1] = '%('
      i = i + 3
    elseif LAZY[c] and nxt == '?' then
      out[#out + 1] = LAZY[c]
      i = i + 2
    elseif c:match('[<>=@%%~&]') then
      out[#out + 1] = '\\' .. c
      i = i + 1
    else
      out[#out + 1] = c
      i = i + 1
    end
  end
  local regex = table.concat(out)
  assert(pcall(vim.regex, regex), ('%s translates to %s, which Vim cannot compile; fix the pattern or the translation'):format(js, regex))
  return regex
end

--- The first line of output the pattern matches, trimmed; nil when none does.
local function first_line(pattern, output)
  assert(type(output) == 'string', ('first_line was given %s output; pass the text the watcher printed'):format(type(output)))
  local re = vim.regex(M.vim_regex(pattern))
  for line in vim.gsplit(output, '\n', { plain = true }) do
    local from, to = re:match_str(line)
    if from then
      local matched = vim.trim(line:sub(from + 1, to))
      assert(output:find(matched, 1, true), ('first_line matched %q, which is not in the output'):format(matched))
      return matched
    end
  end
  return nil
end

--- Each failing test or error the output names, once each, in order. A name is every group of its match that
--- matched, trimmed and joined by a space, so a pattern can name a failure from fields apart on its line.
local function failures_in(watcher, output)
  assert(type(output) == 'string', ('failures_in was given %s output for %s'):format(type(output), watcher.name))
  if watcher.failure == nil then return {} end
  local pattern, names, seen = M.vim_regex(watcher.failure), {}, {}
  for line in vim.gsplit(output, '\n', { plain = true }) do
    local groups = vim.fn.matchlist(line, pattern)
    if #groups > 0 then
      local parts = {}
      for i = 2, 10 do
        if vim.trim(groups[i]) ~= '' then parts[#parts + 1] = vim.trim(groups[i]) end
      end
      local name = table.concat(parts, ' ')
      if name ~= '' and not seen[name] then
        seen[name] = true
        names[#names + 1] = name
      end
    end
  end
  assert(#names == vim.tbl_count(seen), ('%s named %d failures but %d distinct ones'):format(watcher.name, #names, vim.tbl_count(seen)))
  return names
end

--- The latest completed build, unread suffix, and log text with trailing checker diagnostics.
function M.last_build(watcher, output)
  local clean = output:gsub('\27%[[%d;?]*%a', '')
  if watcher.summary == nil then return clean, '', clean end
  local pattern = vim.regex(M.vim_regex(watcher.summary))
  local previous, latest, offset = nil, nil, 0
  for line in vim.gsplit(clean, '\n', { plain = true }) do
    local _, finish = pattern:match_str(line)
    if finish then previous, latest = latest, offset + finish end
    offset = offset + #line + 1
  end
  local start, finish = previous or 0, latest or #clean
  assert(start <= finish, ('%s\'s latest build ends at %d, before the one before it at %d'):format(watcher.name, finish, start))
  local build, unread, text = clean:sub(start + 1, finish), clean:sub(finish + 1), clean:sub(start + 1)
  assert(build .. unread == text, ('%s\'s latest build and what follows it are not its log text'):format(watcher.name))
  return build, unread, text
end

--- The summary of a command that ended with code and printed no summary line: passed, or how it failed.
local function exit_summary(code, failures)
  assert(type(code) == 'number', ('exit_summary was given exit code %s'):format(tostring(code)))
  local summary = code == 0 and 'passed' or (#failures > 0 and ('%d failed'):format(#failures) or ('exited with code %d'):format(code))
  assert((code == 0) == (summary == 'passed'), ('exit code %d gave the summary %q'):format(code, summary))
  return summary
end

--- Reads one build: nil until its summary arrives, or an exit-code result when the command ends without one.
function M.read_report(watcher, output, code)
  local clean, _, text = M.last_build(watcher, output)
  local failures = failures_in(watcher, clean)
  local line = watcher.summary and first_line(watcher.summary, clean)
  local summary, is_failed
  if line then
    summary = (watcher.result and first_line(watcher.result, clean)) or line
    is_failed = watcher.failed ~= nil and vim.regex(M.vim_regex(watcher.failed)):match_str(line) ~= nil
  elseif code == nil or (code == 0 and watcher.summary ~= nil) then
    return nil
  else
    is_failed = code ~= 0
    summary = exit_summary(code, failures)
  end
  local report = { summary = summary, is_failed = is_failed, text = vim.trim(text), failures = is_failed and failures or {} }
  assert(type(report.summary) == 'string' and type(report.is_failed) == 'boolean', ('%s gave summary %s and is_failed %s'):format(watcher.name, vim.inspect(summary), vim.inspect(is_failed)))
  assert(report.is_failed or #report.failures == 0, ('%s passed yet names %d failures'):format(watcher.name, #report.failures))
  return report
end

local DECORATION = { '^Build Summary:%s*', '^zanity:%s*', '^=+%s*', '%s*=+$', '^Test Files%s+', '^Test Suites:%s*', '^DONE%s+', '^ok | ', '^FAILED | ' }

--- The watcher's part of the status line: its name, a tick or a cross, its summary bare, and its first failure.
function M.status_line(watcher, report)
  assert(type(report.summary) == 'string', ('%s\'s report has a %s summary; pass a report read_report gave'):format(watcher.name, type(report.summary)))
  local summary = report.summary
  for _, decoration in ipairs(DECORATION) do
    summary = summary:gsub(decoration, '')
  end
  local failing = ''
  if #report.failures > 0 then
    failing = ': ' .. report.failures[1] .. (#report.failures > 1 and (' +%d more'):format(#report.failures - 1) or '')
  end
  local line = ('overwatch %s %s %s%s'):format(watcher.name, report.is_failed and '✗' or '✓', summary, failing)
  assert(vim.startswith(line, 'overwatch ' .. watcher.name .. ' '), ('the status line %q does not start with its watcher\'s name'):format(line))
  return line
end

return M
