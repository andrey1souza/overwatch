-- Which watchers a project gets: the table every runtime reads, watchers.json at the repository's root, with the
-- settings .overwatch.json gives them.
local M = {}

M.OVERRIDE_PATH = '.overwatch.json'

-- Patterns are JavaScript regexes, as in .overwatch.json; report.vim_regex translates them. Alternatives of one
-- language are tried in order: the first whose marker, and `requires` if it names one, are present runs.
local ROOT = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h:h')
local TABLE = vim.json.decode(table.concat(vim.fn.readfile(ROOT .. '/watchers.json'), '\n'))
M.WATCHERS = TABLE.watchers

-- A language the project's root lacks is looked for in the directories this many levels down, leaving out hidden
-- ones and these.
local SEARCH_DEPTH = TABLE.search.depth
local SEARCH_SKIP = {}
for _, name in ipairs(TABLE.search.skip) do
  SEARCH_SKIP[name] = true
end

--- The language a watcher runs for: its own, or else its name.
local function language_of(watcher)
  assert(type(watcher) == 'table', ('language_of was given a %s, not a watcher; pass an entry of watchers.json'):format(type(watcher)))
  local language = watcher.language or watcher.name
  assert(type(language) == 'string', ('watcher %s has no language or name; give its watchers.json entry a name'):format(vim.inspect(watcher.marker)))
  return language
end

--- The watcher with the project-wide settings of .overwatch.json, which name, run and judge it in place of its own.
local function apply_override(watcher, override)
  assert(type(override) == 'table', ('apply_override was given a %s override; pass {} for none'):format(type(override)))
  local applied = {
    name = override.name or watcher.name,
    language = language_of(watcher),
    marker = watcher.marker,
    argv = override.argv or watcher.argv,
    baseline = override.baseline or watcher.baseline,
    summary = override.summary or watcher.summary,
    failed = override.failed or watcher.failed,
    result = watcher.result,
    failure = watcher.failure,
    dir = watcher.dir,
    env = watcher.env,
  }
  assert(applied.name ~= nil and applied.language == language_of(watcher), ('watcher %s lost its name or language %s'):format(vim.inspect(applied.name), applied.language))
  return applied
end

--- The watcher with the settings .overwatch.json gives its watchers.json name under `watchers`, its env merged over
--- the entry's; nil when they turn it off. A sub-project's watcher takes its entry's settings.
local function configure(watcher, override)
  assert(type(override) == 'table', ('configure was given a %s override; pass {} for none'):format(type(override)))
  local settings = type(override.watchers) == 'table' and override.watchers[watcher.name] or nil
  if type(settings) ~= 'table' then return watcher end
  if settings.enabled == false then return nil end
  local configured = vim.tbl_extend('force', watcher, {
    argv = settings.argv or watcher.argv,
    baseline = settings.baseline or watcher.baseline,
    summary = settings.summary or watcher.summary,
    failed = settings.failed or watcher.failed,
  })
  if type(settings.env) == 'table' then configured.env = vim.tbl_extend('force', watcher.env or {}, settings.env) end
  assert(configured.name == watcher.name and configured.marker == watcher.marker, ('configuring %s renamed it %s or moved its marker; settings change only how it runs'):format(watcher.name, configured.name))
  return configured
end

--- The first of a language's alternatives whose marker is in dir and whose tool, if it needs one, is on PATH.
local function first_present(alternatives, dir)
  assert(vim.startswith(dir, '/'), ('first_present was given %s; pass an absolute directory'):format(dir))
  for _, watcher in ipairs(alternatives) do
    assert(type(watcher.marker) == 'string', ('watcher %s has no marker; give its watchers.json entry one'):format(watcher.name))
    if vim.uv.fs_stat(dir .. '/' .. watcher.marker) and (watcher.requires == nil or vim.fn.executable(watcher.requires) == 1) then
      return watcher
    end
  end
  return nil
end

--- The directories SEARCH_DEPTH levels down, relative to cwd, nearest and then alphabetically first.
local function find_subprojects(cwd)
  assert(vim.startswith(cwd, '/'), ('find_subprojects was given %s; pass the project\'s absolute directory'):format(cwd))
  local found, parents = {}, { '' }
  for depth = 1, SEARCH_DEPTH do
    local level = {}
    for _, parent in ipairs(parents) do
      for name, kind in vim.fs.dir(parent == '' and cwd or (cwd .. '/' .. parent)) do
        if kind == 'directory' and not vim.startswith(name, '.') and not SEARCH_SKIP[name] then
          level[#level + 1] = parent == '' and name or (parent .. '/' .. name)
        end
      end
    end
    table.sort(level)
    for _, dir in ipairs(level) do
      assert(select(2, dir:gsub('/', '')) == depth - 1, ('%s was found at depth %d; each level must go one directory down'):format(dir, depth))
    end
    vim.list_extend(found, level)
    parents = level
  end
  return found
end

--- The project's .overwatch.json, or {} when it has none or it is not a JSON object.
local function read_override(cwd)
  assert(vim.startswith(cwd, '/'), ('read_override was given %s; pass the project\'s absolute directory'):format(cwd))
  local override = {}
  local file = io.open(cwd .. '/' .. M.OVERRIDE_PATH)
  if file then
    local ok, decoded = pcall(vim.json.decode, file:read('*a'))
    file:close()
    if ok and type(decoded) == 'table' then override = decoded end
  end
  assert(type(override) == 'table', ('%s/%s read as a %s, not a table'):format(cwd, M.OVERRIDE_PATH, type(override)))
  return override
end

--- The watchers.json entries for language, in order, with the override's settings; the ones it turns off left out.
local function alternatives_of(language, override)
  local alternatives = {}
  for _, each in ipairs(M.WATCHERS) do
    local configured = language_of(each) == language and configure(each, override) or nil
    if configured then alternatives[#alternatives + 1] = configured end
  end
  assert(#alternatives <= #M.WATCHERS, ('%s has %d alternatives among %d watchers'):format(language, #alternatives, #M.WATCHERS))
  assert(vim.iter(alternatives):all(function(each) return language_of(each) == language end), ('an alternative for %s runs another language'):format(language))
  return alternatives
end

--- The language's watcher in each sub-project whose directory has it, one not inside another that does, each named
--- and run from its directory.
local function in_subprojects(alternatives, language, subprojects, cwd)
  local found, claimed = {}, {}
  for _, dir in ipairs(subprojects) do
    local is_inside = vim.iter(claimed):any(function(parent) return vim.startswith(dir, parent .. '/') end)
    local present = not is_inside and first_present(alternatives, cwd .. '/' .. dir)
    if present then
      claimed[#claimed + 1] = dir
      found[#found + 1] = vim.tbl_extend('force', present, { language = language, name = present.name .. ' ' .. dir, dir = dir })
    end
  end
  assert(#found == #claimed, ('%d %s watchers for %d sub-projects'):format(#found, language, #claimed))
  assert(vim.iter(found):all(function(each) return each.dir ~= nil end), ('a %s sub-project watcher has no directory to run in'):format(language))
  return found
end

--- The watcher .overwatch.json is alone, when it gives a command, a summary and a failure pattern; else none.
local function project_watchers(override)
  local has_command = override.argv ~= nil or (override.baseline ~= nil and #override.baseline > 0)
  if not has_command or override.summary == nil or override.failed == nil then return {} end
  local watchers = { apply_override({ name = 'project', language = 'project', marker = M.OVERRIDE_PATH }, override) }
  assert(watchers[1].summary == override.summary, ('the project watcher reads %s, not %s'):format(watchers[1].summary, override.summary))
  assert(watchers[1].argv ~= nil or watchers[1].baseline ~= nil, 'the project watcher has no command; give .overwatch.json argv or baseline')
  return watchers
end

--- The project's watchers, one for each language it has, or else for each sub-project that has it, one not inside
--- another that does; empty when it has none or its override turns overwatch off. The override applies to the first
--- watcher found, and is the watcher when none is.
function M.find_watchers(cwd)
  local override = read_override(cwd)
  if override.enabled == false then return {} end
  local found, languages, subprojects = {}, {}, nil
  for _, watcher in ipairs(M.WATCHERS) do
    local language = language_of(watcher)
    if not languages[language] then
      languages[language] = true
      local alternatives = alternatives_of(language, override)
      local at_root = first_present(alternatives, cwd)
      subprojects = subprojects or (not at_root and find_subprojects(cwd)) or nil
      for _, each in ipairs(at_root and { at_root } or in_subprojects(alternatives, language, subprojects, cwd)) do
        found[#found + 1] = apply_override(each, #found == 0 and override or {})
      end
    end
  end
  assert(vim.iter(found):all(function(each) return each.name ~= nil end), ('a watcher found in %s has no name'):format(cwd))
  if #found > 0 then return found end
  local project = project_watchers(override)
  assert(#project <= 1, ('%s gave %d project watchers; .overwatch.json makes at most one'):format(cwd, #project))
  return project
end

return M
