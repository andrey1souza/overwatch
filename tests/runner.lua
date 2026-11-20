-- Runs the specs of an `nvim --headless -l` script: each prints whether it passed, and finish raises when any
-- failed, so the script exits non-zero.
local M = { failures = 0 }

--- Runs fn as the spec named name, printing whether it passed and why not.
function M.it(name, fn)
  assert(type(fn) == 'function', ('spec %q has no body; pass it a function'):format(name))
  local before = M.failures
  local ok, err = pcall(fn)
  print((ok and 'pass ' or 'FAIL ') .. name .. (ok and '' or ('\n  ' .. tostring(err))))
  if not ok then M.failures = M.failures + 1 end
  assert(M.failures - before == (ok and 0 or 1), ('spec %q counted %d failures'):format(name, M.failures - before))
end

--- Ends the script, raising when any spec failed so nvim exits non-zero.
function M.finish(what)
  assert(type(what) == 'string', ('finish was given %s; name what the script checked'):format(type(what)))
  assert(M.failures == 0, ('%d %s failed'):format(M.failures, what))
end

return M
