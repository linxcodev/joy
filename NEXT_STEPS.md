# Context Compaction Integration - Next Steps

## Current Status
- ✅ Core compaction modules created
- ✅ Context monitoring display implemented  
- ✅ Configuration system in place
- ⚠️ 398/433 tests passing (92%)
- ❌ 35 tests failing - needs fixes

## Critical Issues to Fix

### 1. Token Counter API Change (Blocks 5+ tests)
**Problem:** `count_conversation()` returns `TokenStats` dataclass, but tests expect int
**Impact:** Tests divide TokenStats by int, causing TypeError
**Files Affected:**
- `src/joy/token_counter.py` - Method returns TokenStats, not int
- `tests/test_comprehensive_context_compaction.py` - Expects int
- `tests/test_token_counter.py` - Uses incorrect return type

**Fix Required:**
```python
# OLD (wrong):
tokens = counter.count_conversation(conversation)  # returns TokenStats
usage_percent = (tokens / available) * 100  # TypeError!

# NEW (correct):
stats = counter.count_conversation(conversation)  # returns TokenStats
usage_percent = (stats.total_tokens / available) * 100
```

### 2. Datetime Deprecation Warnings (3 files)
**Lines to fix:**
- `src/joy/cli.py:712-713` - datetime.utcnow()
- `src/joy/llm.py:227` - datetime.utcnow()
- `src/joy/session_manager.py:351,268,489` - datetime.utcnow()

**Action:** Replace with `datetime.now(datetime.UTC)`

### 3. CLI Integration Missing
**Current:** Context monitor created but not wired into StreamWorker
**Required:**
- Add monitor calls in `process_stream_chunk()`
- Display context % before/after message processing
- Handle compaction triggers non-blocking

### 4. Change Management Tests (4 failures)
**Root Cause:** Session/snapshot integration issues
**Affected:**
- `test_change_management_integration.py` - 4 tests
- `test_comprehensive_compaction_scenarios.py` - 3 tests

### 5. CLI LLM Tests (7 failures)
**Root Cause:** Mock/stub issues with new context monitoring
**Affected:**
- `test_cli.py::LLMTest` - 7 tests
- Missing mock for `ContextMonitor`

## Implementation Priority

### Phase 1: Critical Fixes (30 min)
1. Fix TokenStats usage in tests (5 files)
2. Fix datetime deprecation warnings (3 files)
3. Run smoke tests

### Phase 2: Integration (60 min)
4. Wire ContextMonitor into StreamWorker
5. Add safe error handling for compaction display
6. Update mocks in test suite

### Phase 3: Documentation & Validation (45 min)
7. Create CONTEXT_COMPACTION.md guide
8. Add troubleshooting section
9. Run full test suite
10. Verify CLI smoke test

## Files to Create/Modify

**New Files:**
- `docs/CONTEXT_COMPACTION.md` - Usage guide
- `docs/CONTEXT_CONFIG.md` - Configuration reference
- `examples/context_compaction_demo.py` - Demo script

**Modified Files (priority order):**
1. `tests/test_comprehensive_context_compaction.py` - Fix TokenStats usage
2. `tests/test_token_counter.py` - Fix TokenStats usage
3. `src/joy/cli.py` - Fix datetime + wire monitor
4. `src/joy/llm.py` - Fix datetime
5. `src/joy/session_manager.py` - Fix datetime
6. `tests/test_cli.py` - Update mocks
7. `tests/test_change_management_integration.py` - Fix session issues

## Expected Outcomes
- ✅ 433/433 tests passing (100%)
- ✅ No deprecation warnings
- ✅ Context monitoring displaying in CLI
- ✅ Compaction automatically triggered at threshold
- ✅ Documentation complete
- ✅ Smoke tests passing
