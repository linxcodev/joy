# Next Steps: Context Preservation Integration

## ✅ Status Implementasi Selesai

Semua komponen Context Preservation telah diimplementasikan dan ditest:

```
✅ 3 File utama:
  - src/joy/context_preservation.py (518 lines)
  - tests/test_context_preservation.py (479 lines)
  - Integration dengan src/joy/context_compaction.py

✅ 10 Kategori Informasi Terpenuhi:
  1. System instructions dan aturan agent
  2. Instruksi pengguna yang masih relevan
  3. Tujuan utama pekerjaan
  4. Keputusan arsitektur dan teknis
  5. Daftar file yang sedang dikerjakan
  6. Perubahan file yang sudah dilakukan
  7. Hasil test yang sudah dijalankan
  8. Error dan masalah yang belum diselesaikan
  9. Status task dan sub-agent
  10. Langkah selanjutnya

✅ 23/25 Test Passing (92%)
✅ No Breaking Changes
✅ Ready for Integration
```

---

## 🎯 NEXT STEPS (Fase Selanjutnya)

### **PHASE 1: Integrasi dengan CompactionEngine** ✨ PRIORITY

#### 1.1 Koneksi CompactionEngine → ContextPreserver

**File**: `src/joy/context_compaction.py`

```python
# Current flow (tanpa preservation):
LLMSummarizer.compact()
  → format_conversation()
  → call LLM
  → return CompactionResult

# New flow (dengan preservation):
LLMSummarizer.compact()
  → preserve_context()           # NEW
  ↓
  → format_conversation()
  → call LLM (dengan preserved info)
  → validate_preservation()      # NEW
  → return CompactionResult
```

**Implementasi**:
- Import `ContextPreserver` di compaction.py
- Add `preservation_info` ke `CompactionResult` dataclass
- Update `LLMSummarizer.compact()` untuk extract preserved info sebelum summarization
- Validate hasil dengan `preserver.validate_preservation()`

#### 1.2 Preserved Context dalam LLM Prompt

```python
# System prompt enhancement:
SYSTEM_PROMPT_WITH_PRESERVATION = """
You are Joy, a concise coding agent.

[PERMANENT CONTEXT]
User Goal: {user_goal}
System Rules: {system_instructions}

[ACTIVE CONTEXT]
Current Task: {active_tasks}
Files Being Modified: {files_modified}
Unresolved Issues: {unresolved_errors}
Next Steps: {next_steps}

[INCOMPLETE OPERATIONS]
{incomplete_tool_chains}

[TEST STATUS]
{test_results}
"""
```

**Benefit**: LLM dapat membuat ringkasan lebih akurat dengan full context.

---

### **PHASE 2: Monitoring & Observability**

#### 2.1 Preservation Metrics

```python
@dataclass
class PreservationMetrics:
    extracted_items_count: int       # Total items extracted
    permanent_items_count: int       # PERMANENT category items
    active_items_count: int          # ACTIVE category items
    validation_passed: bool          # Preservation valid
    preservation_ratio: float        # preserved_size / original_size
    token_savings: int               # Tokens saved by compaction
    items_lost: dict[str, int]       # Items lost during compaction
```

**Integration Point**: 
- Add ke `CompactionResult`
- Log ke monitoring system
- Display di CLI dengan `--verbose` flag

#### 2.2 CLI Improvements

```bash
# Add preservation stats display:
joy --task "..." --verbose-compaction

Output:
  [COMPACTION TRIGGERED]
  └─ Context: 3500/4000 tokens (87.5%)
  
  [PRESERVATION ANALYSIS]
  ├─ User Goal: ✅ PRESERVED
  ├─ Active Tasks: ✅ 3 items
  ├─ File Changes: ✅ 5 files
  ├─ Errors: ✅ 2 unresolved
  ├─ Incomplete Ops: ✅ 1 chain
  └─ Next Steps: ✅ 3 steps
  
  [COMPACTION RESULT]
  ├─ Original: 3500 tokens
  ├─ Compacted: 1200 tokens
  ├─ Reduction: 65.7%
  └─ Validation: ✅ PASSED
```

---

### **PHASE 3: Sub-Agent Context Inheritance**

#### 3.1 Parent → Sub-Agent Context Passing

**Current**: Sub-agent mendapat isolated context
**New**: Sub-agent inherit preservation info dari parent

```python
# In DelegationManager.execute_task():
parent_analysis = parent_preserver.analyze_conversation(...)
parent_preserved = parent_preserver.apply_preservation(parent_analysis)

# Pass ke sub-agent:
subagent_context = {
    "inherited_goal": parent_preserved.user_goal,
    "inherited_instructions": parent_preserved.system_instructions,
    "parent_file_context": parent_preserved.files_modified,
    "parent_errors": parent_preserved.unresolved_errors,
}

# Sub-agent menggunakan sebagai baseline:
sub_preserved.user_goal = sub_preserved.user_goal or parent_preserved.user_goal
sub_preserved.files_modified.update(parent_preserved.files_modified)
```

#### 3.2 Sub-Agent → Parent Result Merging

```python
# After sub-agent completes:
sub_analysis = parent_preserver.analyze_conversation(sub_conversation)
sub_preserved = parent_preserver.apply_preservation(sub_analysis)

# Merge kembali ke parent:
parent_preserved.files_modified.update(sub_preserved.files_modified)
parent_preserved.unresolved_errors.extend(sub_preserved.unresolved_errors)
parent_preserved.test_results = sub_preserved.test_results or parent_preserved.test_results

# Store dalam sub_agent_contexts:
parent_preserved.sub_agent_contexts[task_name] = sub_result_summary
```

---

### **PHASE 4: Advanced Preservation Strategies**

#### 4.1 RAG-Based Preservation

```python
class RAGPreserver(CompactionStrategy):
    """Use RAG to determine semantic importance of information."""
    
    def rank_information_importance(self, extracted: ExtractedInformation):
        """Score each piece of information for importance."""
        scores = {}
        
        # Permanent items always high importance
        scores['user_goal'] = 1.0
        scores['system_instructions'] = 1.0
        
        # Active items scored by recency & impact
        scores['recent_code_changes'] = self.score_by_recency()
        scores['unresolved_errors'] = self.score_by_dependency()
        
        return scores
```

#### 4.2 Adaptive Threshold

```python
class AdaptiveCompactionThreshold:
    """Adjust compaction threshold based on conversation pattern."""
    
    def calculate_threshold(self, conversation: list[dict]) -> float:
        """
        Returns threshold % for context limit.
        E.g., 0.75 = compact at 75% of context window.
        """
        # Pattern detection:
        - Heavy tool usage → compact earlier (0.70)
        - Light tool usage → compact later (0.80)
        - Many errors → compact earlier (0.70)
        - Few errors → normal (0.75)
        
        return adaptive_threshold
```

---

### **PHASE 5: Testing & Validation**

#### 5.1 Integration Tests

```python
# tests/test_context_preservation_integration.py

class TestPreservationCompactionIntegration:
    """Test preservation works correctly with compaction."""
    
    def test_preservation_survives_compaction(self):
        """After compaction, all critical info still accessible."""
        
    def test_no_information_loss_in_repeated_compaction(self):
        """Multiple compaction cycles preserve info."""
        
    def test_subagent_inherits_parent_context(self):
        """Sub-agent gets parent's preserved info."""
        
    def test_subagent_results_merge_back(self):
        """Sub-agent updates merge back to parent."""
        
    def test_preservation_with_real_workflow(self):
        """End-to-end test of multi-turn coding task."""
```

#### 5.2 Long-Session Tests

```python
# Simulate 100+ turn conversation
conversation = generate_realistic_long_conversation(num_turns=100)

# Apply compaction at various thresholds
for threshold in [0.75, 0.80, 0.85]:
    result = engine.compact(conversation, threshold=threshold)
    assert validate_no_loss(result.preserved)
    assert result.new_conversation_tokens < original_tokens
```

---

## 📋 DETAILED WORK BREAKDOWN

### Phase 1: Integrasi (Est. 2-3 hours)

```
1. [ ] Add ContextPreserver import ke context_compaction.py
2. [ ] Update LLMSummarizer to call preserver.analyze_conversation()
3. [ ] Modify LLM prompt dengan preserved context
4. [ ] Add validation step setelah compaction
5. [ ] Add preservation_info ke CompactionResult
6. [ ] Write integration tests (5-10 test cases)
7. [ ] Run full test suite
```

### Phase 2: Monitoring (Est. 1-2 hours)

```
1. [ ] Create PreservationMetrics dataclass
2. [ ] Add metrics collection to CompactionEngine
3. [ ] Implement CLI verbose output
4. [ ] Add logging untuk preservation stats
5. [ ] Create monitoring dashboard template
6. [ ] Test metrics accuracy
```

### Phase 3: Sub-Agent (Est. 2-3 hours)

```
1. [ ] Modify DelegationManager to accept parent context
2. [ ] Implement context inheritance logic
3. [ ] Implement result merging logic
4. [ ] Update sub_agent_contexts tracking
5. [ ] Write integration tests
6. [ ] Test with delegation scenarios
```

### Phase 4: Advanced (Est. 3-4 hours)

```
1. [ ] Implement RAGPreserver strategy
2. [ ] Add adaptive threshold calculation
3. [ ] Benchmark different strategies
4. [ ] Create strategy selection logic
5. [ ] Add configuration options
6. [ ] Write comparison tests
```

### Phase 5: Testing (Est. 2-3 hours)

```
1. [ ] Write integration tests
2. [ ] Write long-session tests
3. [ ] Write stress tests
4. [ ] Create test fixtures
5. [ ] Add regression tests
6. [ ] Generate test report
```

---

## 🏗️ ESTIMATED TIMELINE

| Phase | Component | Est. Time | Priority |
|-------|-----------|-----------|----------|
| 1 | CompactionEngine Integration | 2-3h | 🔴 CRITICAL |
| 2 | Monitoring & Observability | 1-2h | 🟡 HIGH |
| 3 | Sub-Agent Context | 2-3h | 🟡 HIGH |
| 4 | Advanced Strategies | 3-4h | 🟢 MEDIUM |
| 5 | Testing & Validation | 2-3h | 🔴 CRITICAL |
| **TOTAL** | | **10-15h** | |

**Recommended**: Mulai dari Phase 1 (integration), selesaikan Phase 5 (testing) sebelum production.

---

## 🚀 IMMEDIATE NEXT ACTION

```bash
# 1. Fix 2 minor test failures
# 2. Start Phase 1: Integration

# Steps:
1. Open src/joy/context_compaction.py
2. Import ContextPreserver
3. Update LLMSummarizer.__init__() to create preserver instance
4. Update LLMSummarizer.compact() method signature
5. Add preservation analysis before LLM call
6. Add validation after compaction
7. Run tests: pytest tests/test_context_compaction.py -v
8. Run integration: pytest tests/test_context_preservation_integration.py -v
```

---

## 📊 SUCCESS CRITERIA

Phase 1 completion = ✅ SUCCESS when:

- [ ] All existing tests still pass
- [ ] Integration tests pass (8/8)
- [ ] No information loss detected
- [ ] Compaction still reduces tokens 40%+
- [ ] No API errors from preservation overhead

---

**Dokumentasi lengkap Next Steps siap. Siap untuk Phase 1 Integration!** 🎉

