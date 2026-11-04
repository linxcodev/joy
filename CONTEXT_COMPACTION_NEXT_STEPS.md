# Context Compaction Integration - Next Steps & Fixes

**Status**: 92% Implementation (399/433 tests passing)

## 🎯 Priority 1: Critical Fixes (Must Fix)

### 1.1 Fix `SessionManager` Path Type Mismatch
**File**: `src/joy/session_manager.py:128`
**Issue**: `session_dir` is string but `.mkdir()` called on it
**Fix**: Ensure `session_dir` is converted to `pathlib.Path`
```python
# Line 128 - should be:
self.session_dir = Path(session_dir)  # Ensure Path object
```

### 1.2 Fix `TokenStats` Type Comparisons
**Files**: 
- `src/joy/context_preservation.py:163`
- `tests/test_token_counter.py`
**Issue**: `TokenStats` object compared with `int/float`
**Fix**: Add proper comparison methods or use `.total_tokens` attribute
```python
# Current (wrong):
if stats > 0.75:  # TypeError

# Correct:
if stats.total_tokens > threshold:
```

### 1.3 Fix Deprecated `datetime.utcnow()`
**Files**: 
- `src/joy/session_manager.py:351, 358, 489`
- `src/joy/llm.py`
- `src/joy/cli.py`
**Fix**: Replace with `datetime.now(datetime.UTC)`
```python
# Old:
datetime.utcnow().isoformat()

# New:
datetime.now(datetime.UTC).isoformat()
```

### 1.4 Fix `CompactionEngine` Missing Attribute
**File**: `src/joy/context_compaction.py`
**Issue**: Tests expect `_compactor` attribute
**Fix**: Add proper attribute initialization or factory method

### 1.5 Fix `ContextPreserver.apply_preservation()` Signature
**File**: `src/joy/context_preservation.py:275`
**Issue**: Missing required `conversation` argument
**Fix**: Verify method signature matches test expectations

---

## 🎯 Priority 2: Integration Points (Make It Work)

### 2.1 Wire Context Monitor to CLI Main Loop
**File**: `src/joy/cli.py`
**Current**: Imports exist but not called
**Action**: 
```python
# In main CLI loop / StreamWorker:
1. Create ContextMonitor instance
2. Calculate metrics after each turn
3. Call display_context_usage() periodically
4. Trigger compaction check
```

### 2.2 Integrate CompactionEngine to StreamWorker
**File**: `src/joy/execution.py` (StreamWorker class)
**Action**:
```python
# In stream_worker or tool executor:
1. Import CompactionEngine
2. Check if compaction_needed() before each tool call
3. Perform compaction if threshold exceeded
4. Update conversation history
5. Display compaction status via monitor
```

### 2.3 Wire Compaction Config to CLI Commands
**File**: `src/joy/cli.py`
**Action**:
```python
# Add CLI flags:
--enable-compaction / --disable-compaction
--compaction-threshold 0.75
--max-summary-tokens 2000

# Read from ENV:
JOY_COMPACTION_ENABLED
JOY_COMPACTION_THRESHOLD
JOY_MAX_SUMMARY_TOKENS
```

---

## 🎯 Priority 3: Test Fixes

### 3.1 Fix `test_comprehensive_context_compaction.py`
**Root Issues**:
- `ExtractedInformation` extraction not capturing details
- `SessionManager._session_dir` attribute access
- `TokenStats` comparison operators
- Missing attributes in mock objects

**Strategy**: 
1. Fix type comparisons first
2. Fix mock object initialization
3. Verify extraction logic captures all fields

### 3.2 Fix `test_token_counter.py`
**Issues**:
- Model context window size mismatches (8192 vs 128000)
- Missing `available_output_tokens` in result dict
- Estimation flag handling

**Strategy**:
1. Verify model config for correct context windows
2. Ensure TokenStats includes all expected fields
3. Test with both estimated and tiktoken modes

### 3.3 Fix `test_context_preservation.py`
**Issues**:
- `apply_preservation()` signature mismatch
- File extraction not working
- Missing required arguments

**Strategy**:
1. Align method signatures
2. Verify extraction patterns
3. Test with sample conversations

---

## 📋 Implementation Checklist

### Phase 1: Core Fixes (1-2 hours)
- [ ] Fix `SessionManager` path type issue
- [ ] Fix `TokenStats` comparison operators  
- [ ] Replace deprecated `datetime.utcnow()` calls
- [ ] Fix `CompactionEngine` attributes
- [ ] Verify `ContextPreserver` signatures

### Phase 2: CLI Integration (2-3 hours)
- [ ] Wire `ContextMonitor` to main CLI loop
- [ ] Add compaction config via CLI args
- [ ] Integrate `CompactionEngine` to `StreamWorker`
- [ ] Add display calls at appropriate points
- [ ] Ensure non-blocking behavior

### Phase 3: Test Cleanup (1-2 hours)
- [ ] Fix all failing context compaction tests
- [ ] Fix all token counter tests
- [ ] Fix context preservation tests
- [ ] Run full test suite
- [ ] Verify 95%+ pass rate

### Phase 4: Documentation (30 min)
- [ ] Add context compaction guide
- [ ] Document CLI flags
- [ ] Add troubleshooting section
- [ ] Example usage scenarios

---

## 🔧 Code Changes Summary

| File | Changes | Lines |
|------|---------|-------|
| `session_manager.py` | Path type fix, datetime fix | 5-10 |
| `context_preservation.py` | TokenStats comparison, signature | 5-10 |
| `token_counter.py` | Add comparison ops, fix result dict | 10-15 |
| `context_compaction.py` | Add attributes, initialization | 5-10 |
| `cli.py` | Add display calls, config integration | 20-30 |
| `execution.py` | Add compaction check/trigger | 15-20 |
| Tests (multiple) | Fix mocks, assertions, configs | 50-100 |

**Total Estimated**: 150-200 lines of changes

---

## ⚙️ Configuration Reference

### Environment Variables
```bash
# Enable/disable automatic compaction
JOY_COMPACTION_ENABLED=true

# Trigger compaction at X% context usage (0-1.0)
JOY_COMPACTION_THRESHOLD=0.75

# Maximum tokens allowed in compacted summary
JOY_MAX_SUMMARY_TOKENS=2000

# Model to use for summarization (must have valid config)
JOY_COMPACTION_MODEL=gpt-4

# Session persistence
JOY_SESSION_ENABLED=true
JOY_SESSION_DIR=~/.joy/sessions
```

### CLI Flags
```bash
# Disable compaction for this session
joy --no-compaction

# Set custom threshold
joy --compaction-threshold 0.8

# Verbose compaction logging
joy --debug-compaction

# Show context usage on every turn
joy --show-context-usage
```

---

## 🧪 Smoke Test Plan

```bash
# 1. Run test suite (should be >90% passing)
pytest tests/ -q

# 2. Manual CLI test
export JOY_COMPACTION_ENABLED=true
export JOY_COMPACTION_THRESHOLD=0.75

# Start CLI and verify:
# - Context usage displays on startup
# - Compaction triggered when threshold reached
# - Displays show properly without interrupting stream
# - No errors in logs

# 3. Verify config
joy --help | grep -i compaction
env | grep JOY_COMPACTION

# 4. Test with long conversation (forces compaction)
# Simulate 30+ exchanges to trigger compaction
```

---

## 📊 Expected Results After Fixes

```
Test Results:
✅ PASSED: 420+ (97%+)
❌ FAILED: <5 (3%)
⚠️  WARNINGS: Addressed deprecations

CLI Output (Sample):
🟢 Context Usage: 32% (12,800 / 40,000 tokens)

[After long conversation...]
🟡 Context Usage: 87% (34,800 / 40,000 tokens)
⚡ Context threshold reached
🔄 Compacting conversation...
✓ Context compacted
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ Metric            ┃ Value            ┃
┡━━━━━━━━━━━━━━━━━━━┩
│ Original tokens   │ 34,800           │
│ Compacted tokens  │ 9,200            │
│ Reduction         │ 25,600 (73.6%)   │
└───────────────────┴──────────────────┘

🟢 Context Usage: 23% (9,200 / 40,000 tokens)
```

---

## 🚀 Recommended Implementation Order

1. **Day 1 Morning**: Fix critical type/attribute issues (Priority 1)
2. **Day 1 Afternoon**: Run tests, fix remaining test failures
3. **Day 2 Morning**: Implement CLI integration (Priority 2)
4. **Day 2 Afternoon**: Smoke testing & documentation
5. **Day 3**: Final verification & optimization

**Total Effort**: 2-3 working days

---

## ⚠️ Known Issues & Workarounds

| Issue | Impact | Workaround |
|-------|--------|-----------|
| SessionManager path type | Session load/save fails | Convert to Path immediately |
| TokenStats comparison | Threshold checks fail | Use `.total_tokens` attribute |
| Deprecated datetime | Future Python incompatibility | Update all utcnow() calls |
| CompactionEngine attributes | Tests fail | Initialize all attributes in `__init__` |
| Context monitor display | Not showing | Add to CLI loop explicitly |

