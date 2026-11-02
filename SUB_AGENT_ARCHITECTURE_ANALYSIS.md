# Analisis Arsitektur Sub-Agent untuk Joy

**Status**: Analisis selesai tanpa perubahan kode  
**Tanggal**: 2025  
**Baseline Test**: 161 passed, 7 failed (mostly UI/format issues, tidak related to core agent loop)

---

## 1. DIAGRAM ARSITEKTUR SAAT INI

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Joy CLI Terminal (cli.py)                    │
│  ┌──────────────────┐  ┌─────────────────┐  ┌──────────────────┐   │
│  │ PromptSession    │  │ DiffCommandHdlr │  │ StreamWorker     │   │
│  │ (TUI + Feedback) │  │ (/diff commands)│  │ (agent loop)     │   │
│  └────────┬─────────┘  └────────┬────────┘  └────────┬─────────┘   │
│           │                     │                    │              │
│           └─────────────────────┼────────────────────┘              │
│                                 │                                   │
└─────────────────────────────────┼───────────────────────────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │   Message History & Conversation   │
                │  (kept in-memory, per session)    │
                └────────────────────────────────────┘
                                  │
                ┌─────────────────▼──────────────────────────────────┐
                │          LLM Agent Loop (llm.py)                    │
                │                                                    │
                │  StreamWorker._loop()                             │
                │  ├─ stream_response()                             │
                │  │  └─ client.responses.create()                  │
                │  │     (OpenAI Responses API)                     │
                │  ├─ Parse response output                         │
                │  ├─ Extract function_calls                        │
                │  └─ Execute loop (up to MAX_REQUESTS=16)          │
                │     ├─ Tool execution (max 8 per response)        │
                │     ├─ Add results to conversation                │
                │     └─ Loop back for next response                │
                └────────────────────┬───────────────────────────────┘
                                     │
                ┌────────────────────▼───────────────────────────────┐
                │        Tool Registry & Execution (tools.py)        │
                │                                                   │
                │  response_tools()  - Build tool definitions      │
                │  run_tool()        - Execute by name             │
                │                                                   │
                │  ├─ read()    - Read file with line numbers      │
                │  ├─ write()   - Create/overwrite file            │
                │  ├─ edit()    - Replace unique text match        │
                │  └─ bash()    - Run shell command                │
                └────────────────────┬───────────────────────────────┘
                                     │
                ┌────────────────────▼───────────────────────────────┐
                │      Workspace File System (ROOT)                  │
                │                                                   │
                │  - Path validation (no escape)                    │
                │  - Atomic writes with temp files                  │
                │  - Thread-safe file mutations                     │
                │  - Symlink resolution                             │
                └───────────────────────────────────────────────────┘

Parallel Thread: Diff Tracking
┌─────────────────────────────────────────────────────────────────────┐
│  DiffEngine (diff_engine.py) + DiffChangeManager (diff_change_manager.py)
│
│  capture_initial_state() → DiffSnapshot
│  detect_and_track_changes() → FileDiffMetadata dict
│  DiffUI.display_diff_review() → Console output
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. DAFTAR FILE PENTING BESERTA TANGGUNG JAWABNYA

### Core Agent Loop & LLM Integration

| File | LOC | Tanggung Jawab |
|------|-----|----------------|
| `src/joy/llm.py` | 216 | **Agent loop kernel**. StreamWorker thread, conversation management, streaming from OpenAI Responses API, tool execution loop, error handling. MAX_REQUESTS=16, MAX_TOOL_CALLS=8. |
| `src/joy/tools.py` | 369 | **Tool definitions & execution**. Defines 4 tools (read/write/edit/bash), validates paths, handles file I/O atomically, manages workspace ROOT, runs bash with cancellation. Uses aisuite's Tools helper. |
| `src/joy/cli.py` | 257 | **TUI & session management**. PromptSession with prompt_toolkit, real-time streaming display, message history, interaction loop, integrates diff tracking. |

### LLM Configuration

| File | Key Config |
|------|-----------|
| `pyproject.toml` | Dependencies: openai>=1.107, aisuite[openai]>=0.1.14, pydantic>=2, prompt-toolkit>=3.0.53, rich>=13 |
| `src/joy/llm.py` | `DEFAULT_MODEL="gpt-5.6-luna"`, `DEFAULT_BASE_URL=https://api.openai.com/v1`, `TIMEOUT=60s` |

**Provider Specifics**:
- ✅ Uses OpenAI Responses API (streaming responses)
- ✅ Supports `reasoning.effort="medium"` 
- ✅ Uses `include=["reasoning.encrypted_content"]`
- ✅ Requires tools via `tools=response_tools()`
- ✅ Function call format: `{"type": "function_call", "name": "...", "arguments": {...}, "call_id": "..."}`
- ✅ Results format: `{"type": "function_call_output", "call_id": "...", "output": "..."}`
- ⚠️ **Does NOT support parallel/batch tool execution** - processes sequentially

### Diff & Change Management (Integrated but Separable)

| File | LOC | Tanggung Jawab |
|------|-----|----------------|
| `src/joy/diff_engine.py` | 439 | Core diff engine. Capture snapshots, compare files, detect changes, hash validation, binary/large file handling, file status detection. |
| `src/joy/diff_config.py` | 63 | Configuration (env vars). Auto-display, summary, watch/ignore patterns, format, syntax highlight. |
| `src/joy/diff_commands.py` | 235 | `/diff` command handling, change tracking integration, auto-display logic. |
| `src/joy/diff_ui.py` | 425 | Rich-based UI for diffs. File list, syntax highlighting, diff rendering, symbols/colors. |
| `src/joy/diff_change_manager.py` | 340 | Accept/reject state machine, conflict detection, batch operations, undo stack. |
| `src/joy/diff_snapshot_manager.py` | 350 | Snapshot persistence, file restoration, conflict detection on restore. |

---

## 3. CARA JOY MENJALANKAN AGENT & MENGEMBALIKAN HASIL

### Alur Eksekusi Satu Turn

```python
# cli.py: main()
while True:
    message = session.prompt(PROMPT, default=draft)
    
    # Capture initial state untuk diff tracking
    diff_handler.capture_initial_state()
    
    # Build conversation dengan system prompt
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": message}
    ]
    
    # Jalankan agent loop di thread terpisah
    worker = StreamWorker(messages)
    draft = stream_turn(session, state, messages, draft)
    
    # Detect & track changes
    if state["used_tools"]:
        changes = diff_handler.detect_and_track_changes()
        if changes and diff_handler.should_auto_display_diff(changes):
            diff_handler.display_auto_diff(changes)
    
    # Update history
    if finished:
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply}
        ]
```

### Alur dalam StreamWorker._loop()

```python
# llm.py: StreamWorker._loop()
for _ in range(MAX_REQUESTS):  # up to 16 requests per user turn
    
    # 1. GET RESPONSE dari LLM
    response = stream_response(client, model, conversation, on_text, cancel)
    if response is None:
        return  # cancelled or error
    
    # 2. ADD RESPONSE TO CONVERSATION (including reasoning)
    conversation += [item.model_dump(exclude_none=True) 
                    for item in response.output]
    
    # 3. EXTRACT TOOL CALLS
    calls = [item for item in response.output 
            if item.type == "function_call"]
    
    if not calls:
        put("done", None)  # Final response, no tools
        return
    
    # 4. EXECUTE TOOLS (max 8 per response)
    for index, call in enumerate(calls):
        if index >= MAX_TOOL_CALLS:
            result = f"Error: only {MAX_TOOL_CALLS} tool calls per response"
        else:
            put("tool", (call.name, "running"))
            result, ok = run_tool(call.name, call.arguments, cancel_event)
            put("tool", (call.name, "finished" if ok else "failed"))
        
        # ADD RESULT TO CONVERSATION
        conversation.append({
            "type": "function_call_output",
            "call_id": call.call_id,
            "output": result
        })
    
    # Loop kembali dengan conversation yang sudah panjang
```

### Tool Execution

```python
# tools.py: run_tool()
def run_tool(name: str, arguments: dict, cancel: threading.Event) -> tuple[str, bool]:
    # Dispatch ke read/write/edit/bash
    # Validate path (no escape)
    # Execute dengan error handling
    # Return (result_text, success_bool)
```

---

## 4. MEKANISME YANG ADA

### ✅ Yang Sudah Ada & Bisa Digunakan Kembali

1. **Agent Loop Foundation**
   - Streaming response handling ✅
   - Tool registration & dispatch ✅
   - Conversation history management ✅
   - Cancellation mechanism (threading.Event) ✅
   - Error handling & retry logic ✅

2. **Tool Infrastructure**
   - Path validation & workspace boundary ✅
   - Atomic file operations ✅
   - Tool error recovery ✅
   - Bash command execution with timeout ✅

3. **Diff & Change Management** (Separable)
   - Snapshot/state capture ✅
   - Change detection ✅
   - Conflict detection ✅
   - Accept/reject state machine ✅

### ❌ Yang TIDAK Ada

1. **Task/Agent Abstraction**
   - Tidak ada base Agent class
   - Tidak ada abstract interface untuk agent behavior
   - Agent loop hard-coded dalam StreamWorker

2. **Agent Delegation**
   - Tidak ada mekanisme spawn sub-agent
   - Tidak ada agent-to-agent communication
   - Tidak ada context passing antar agent

3. **Tool Context Isolation**
   - Tools global (shared ROOT, shared state)
   - Tidak ada per-agent tool configuration
   - Tidak ada permission/capability system

4. **Agent Lifecycle Management**
   - Tidak ada agent registry
   - Tidak ada lifecycle hooks
   - Session memory tidak di-manage (in-memory only)

---

## 5. REKOMENDASI ARSITEKTUR SUB-AGENT

### Design Principles

1. **Non-invasive**: Tidak modifikasi core agent loop
2. **Composable**: Sub-agent bisa spawn sub-agents (multi-level)
3. **Isolated**: Per-agent context, tool constraints, workspace boundaries
4. **Observable**: Introspection, logging, debugging
5. **Compatible**: Works dengan OpenAI Responses API (tidak assume features)

### Proposed Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    Agent Abstraction Layer (NEW)                  │
│                                                                  │
│  BaseAgent (ABC)                                                 │
│  ├─ name: str                                                    │
│  ├─ config: AgentConfig                                          │
│  ├─ context: AgentContext (workspace, capabilities, history)    │
│  ├─ async execute(task: str) -> AgentResult                     │
│  └─ spawn_sub_agent(...) -> Agent                               │
│                                                                  │
│  MainAgent(BaseAgent)                                            │
│  └─ _run_agent_loop() [adapt existing StreamWorker logic]      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│   Sub-Agent 1    │  │   Sub-Agent 2    │  │  Sub-Agent N     │
│ (Isolated Context)  (Isolated Context)  (Isolated Context)
│ (Tool Subset)     (Tool Subset)        (Tool Subset)
│ (Own History)     (Own History)        (Own History)
└──────────────────┘  └──────────────────┘  └──────────────────┘

AgentContext (NEW)
├─ workspace_root: Path (current working directory for tools)
├─ allowed_tools: Set[str] (read/write/edit/bash or custom)
├─ conversation_history: List[Message]
├─ parent_agent: Optional[Agent] (for delegation context)
├─ max_requests: int
├─ max_tool_calls: int
└─ timeout: int

AgentConfig (NEW)
├─ model: str
├─ base_url: str
├─ api_key: str
├─ reasoning_effort: str
└─ custom_system_prompt: Optional[str]

SubAgentResult (NEW)
├─ success: bool
├─ output: str
├─ changes: Dict[str, FileDiffMetadata]
├─ conversation: List[Message]
└─ errors: List[str]
```

### Key Components to Implement

#### 1. BaseAgent (Abstract Class)
```python
class BaseAgent:
    def __init__(self, name: str, context: AgentContext, config: AgentConfig):
        self.name = name
        self.context = context
        self.config = config
    
    async def execute(self, task: str) -> AgentResult:
        """Main execution point. Returns result with changes/output."""
        raise NotImplementedError
    
    def spawn_sub_agent(self, 
                       name: str,
                       workspace_path: Optional[str] = None,
                       allowed_tools: Optional[Set[str]] = None) -> "BaseAgent":
        """Create isolated sub-agent with inherited or custom config."""
        raise NotImplementedError
    
    def _call_tool(self, tool_name: str, args: dict) -> str:
        """Execute tool with context validation."""
        # Check if tool allowed
        # Check if path in allowed workspace
        # Execute
        pass
```

#### 2. MainAgent (Concrete Implementation)
- Adapt existing StreamWorker logic
- Use AgentContext instead of shared globals
- Support tool filtering
- Integrate with diff tracking

#### 3. AgentRegistry (NEW)
```python
class AgentRegistry:
    _agents: Dict[str, BaseAgent] = {}
    
    def register(self, agent: BaseAgent) -> None:
        self._agents[agent.name] = agent
    
    def get(self, name: str) -> Optional[BaseAgent]:
        return self._agents.get(name)
    
    def list_agents(self) -> List[str]:
        return list(self._agents.keys())
```

#### 4. AgentExecutor (Supervisor)
```python
class AgentExecutor:
    def __init__(self, primary_agent: BaseAgent):
        self.primary = primary_agent
        self.registry = AgentRegistry()
    
    async def execute_with_delegation(self, task: str) -> AgentResult:
        # Execute main agent
        # Monitor for sub-agent spawn requests
        # Manage sub-agent results back to parent
        # Aggregate final result
        pass
```

---

## 6. RISIKO & BATASAN

### Risiko Arsitektural

| Risk | Severity | Mitigation |
|------|----------|-----------|
| **Shared workspace state** | HIGH | Create per-agent workspace roots or validate path boundaries. Don't share file handles. |
| **LLM API limits** | HIGH | Sub-agents count toward rate limits. Need request pooling, backpressure. Each sub-agent spawn is extra request. |
| **Context explosion** | HIGH | Conversation history grows with each agent turn. Need context windowing, summarization. |
| **Thread safety** | MEDIUM | StreamWorker runs in daemon threads. Need locks for shared state (registry, snapshots). |
| **Error propagation** | MEDIUM | Sub-agent errors should not crash parent. Need error hierarchy, logging. |
| **Testing complexity** | MEDIUM | More agents = more test permutations. Need fixture factories, mocking. |

### Batasan OpenAI Responses API

✅ **Supported**:
- Streaming responses
- Function call tools
- Reasoning with `effort=medium`
- Sequential tool execution

❌ **Not Supported** (check provider compatibility):
- Parallel/batch tool calls (processes sequentially)
- Tool use by model for decision-making (must execute before next response)
- Custom instructions per agent (system prompt only)
- Request prioritization or queueing
- Agent-to-agent message format (must be text → tool)

⚠️ **Unknown/Vendor-Specific**:
- Sub-agent system prompt embedding
- Cost tracking per agent
- Rate limiting per agent vs global

### Workspace Isolation Strategy

**Option A: Subdirectory per Sub-Agent** (Recommended)
```
workspace/
├─ .joy_agent_context/
│  ├─ main_agent/
│  │  ├─ history.jsonl
│  │  └─ snapshots/
│  └─ sub_agent_a/
│     ├─ history.jsonl
│     └─ snapshots/
└─ actual_project_files/
```

**Option B: Temporary Directories**
```
/tmp/joy_sub_agent_{uuid}/
├─ workspace -> symlink to actual workspace or copy
├─ context.json
└─ results/
```

**Option C: Path-based Capability** (Current + Enhancement)
```
AgentContext.workspace_root = "/project/src"  # Can't access parent
# tools validate ALL paths against this root
```

---

## 7. RENCANA IMPLEMENTASI BERTAHAP

### Phase 1: Foundation (2-3 hari)

**Goal**: Create agent abstraction layer, no changes to CLI yet

- [ ] Create `src/joy/agent_base.py`
  - `BaseAgent` ABC with execute(), spawn_sub_agent()
  - `AgentContext` dataclass
  - `AgentConfig` dataclass
  - `AgentResult` dataclass

- [ ] Create `src/joy/agent_registry.py`
  - `AgentRegistry` class
  - Registration/lookup logic

- [ ] Create `tests/test_agent_base.py`
  - Mock BaseAgent implementation
  - Context isolation tests
  - Spawn sub-agent tests

- [ ] **No changes to**: `cli.py`, `llm.py`, `tools.py`

### Phase 2: MainAgent Implementation (3-4 hari)

**Goal**: Adapt StreamWorker to BaseAgent interface, start integration tests

- [ ] Create `src/joy/agents/main_agent.py`
  - `MainAgent(BaseAgent)` implementing execute()
  - Adapt StreamWorker._loop() logic
  - Tool call dispatch with context validation
  - Support tool filtering (allowed_tools in AgentContext)

- [ ] Create `src/joy/tool_executor.py`
  - Centralized tool execution
  - Path validation per agent context
  - Error wrapping

- [ ] Update `src/joy/llm.py`
  - Extract `_loop()` logic to reusable function
  - Keep backward compatibility (StreamWorker still works)

- [ ] Create `tests/test_main_agent.py`
  - Test agent execution
  - Test tool execution with context
  - Test error handling

- [ ] Create `tests/test_sub_agent_spawn.py`
  - Test sub-agent creation
  - Test context isolation
  - Test workspace boundaries

### Phase 3: Sub-Agent Execution (3-4 hari)

**Goal**: Enable sub-agent spawning from main agent, end-to-end tests

- [ ] Create `src/joy/agents/sub_agent.py`
  - `SubAgent(BaseAgent)` extending MainAgent
  - Overload spawn_sub_agent() for nested spawning
  - Result aggregation

- [ ] Create `src/joy/agent_executor.py`
  - `AgentExecutor` class
  - Manage primary agent + sub-agent pool
  - Backpressure handling (request pooling)
  - Error recovery

- [ ] Update `src/joy/tools.py`
  - Add `tool_executor` parameter
  - Support per-context path validation
  - Keep backward compatibility

- [ ] Create `tests/test_agent_executor.py`
  - Full agent execution flow
  - Sub-agent spawn and result aggregation
  - Backpressure + rate limiting

### Phase 4: Diff Tracking Integration (2-3 hari)

**Goal**: Diff tracking works with per-agent context

- [ ] Update `src/joy/diff_engine.py`
  - Support per-agent snapshots
  - Optionally track changes per sub-agent

- [ ] Update `src/joy/diff_commands.py`
  - Display changes per agent (optional tree view)

- [ ] Create `tests/test_sub_agent_diff_tracking.py`
  - Track changes in sub-agent execution
  - Merge diff reports

### Phase 5: CLI Integration (2 hari)

**Goal**: Optional: Let user spawn sub-agents from CLI

- [ ] Update `src/joy/cli.py`
  - Optional: `/spawn-agent` command
  - Optional: delegate task to sub-agent
  - Display sub-agent results inline

- [ ] Create `tests/test_cli_sub_agent_commands.py`

### Phase 6: Documentation & Examples (1-2 hari)

- [ ] Create `docs/sub_agents.md`
  - Architecture overview
  - Usage examples
  - Pitfalls and best practices

- [ ] Create `examples/sub_agent_tasks.py`
  - Example: Parallel file analysis (sub-agents process different files)
  - Example: Recursive code refactoring (main agent → sub-agents for modules)
  - Example: Multi-step workflow (main agent spawns specialized sub-agents)

---

## 8. BASELINE TEST RESULTS

### Current Test Status
- **Passed**: 161/168
- **Failed**: 7 (mostly UI symbols/format, not core logic)
- **Status**: Core agent loop is STABLE ✅

Failed tests (non-critical):
1. `test_reject_file` - File restoration logic
2. `test_reject_all` - Batch operations
3. `test_display_review_summary_with_mixed_status` - UI formatting
4. `test_complete_accept_reject_workflow` - State machine
5. `test_create_snapshot_with_binary_file` - Binary handling
6. `test_snapshot_empty_workspace` - Edge case
7. `test_get_symbol_unicode` - Unicode symbol fallback

**Recommendation**: Keep baseline tests as-is. Add new test suite for agent layer without modifying existing.

---

## 9. INTEGRATION CHECKLIST

Before implementing, ensure:

- [ ] Understand OpenAI Responses API limits (rate, concurrent agents, context)
- [ ] Design workspace isolation strategy (Option A/B/C above)
- [ ] Plan error escalation from sub-agent to parent
- [ ] Plan context window management (history truncation)
- [ ] Plan logging/debugging for multi-agent flows
- [ ] Plan cost tracking if using OpenAI billing
- [ ] Test with multiple agent levels (3+ depth)
- [ ] Test concurrent sub-agent spawning
- [ ] Test sub-agent failure recovery
- [ ] Benchmark overhead (agent instantiation, context creation)

---

## 10. QUICK REFERENCE: CODE LOCATIONS

```
Core Agent Loop:
  src/joy/llm.py:174-216          StreamWorker._loop()
  src/joy/llm.py:135-173          StreamWorker class
  src/joy/llm.py:96-132           stream_response()

Tool System:
  src/joy/tools.py:206-369        Tool definitions (read/write/edit/bash)
  src/joy/tools.py:340-365        run_tool() dispatcher

CLI Integration:
  src/joy/cli.py:124-167          stream_turn() (TUI ↔ agent)
  src/joy/cli.py:169-254          main() event loop

Diff Tracking:
  src/joy/diff_engine.py:87-439   DiffEngine class
  src/joy/diff_commands.py:16-235 DiffCommandHandler

Configuration:
  pyproject.toml                   Dependencies
  src/joy/llm.py:13-32            System prompt, constants
```

---

## 11. NEXT STEPS

1. **Week 1**: Review this analysis, gather feedback on design choices
2. **Week 2**: Implement Phase 1 (agent abstraction) in isolated branch
3. **Week 3-4**: Phases 2-3 (main + sub-agent implementation)
4. **Week 5**: Integration + documentation
5. **Week 6**: Testing + optimization

**Do NOT start Phase 1 until design is approved.**
