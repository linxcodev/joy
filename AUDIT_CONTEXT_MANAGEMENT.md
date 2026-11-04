# AUDIT MENYELURUH: SISTEM PENGELOLAAN CONTEXT - JOY CODING AGENT

**Status**: Completed  
**Date**: 2024  
**Scope**: Full codebase analysis tanpa modifikasi  

---

## EXECUTIVE SUMMARY

Joy Agent menggunakan **streaming API dan conversation history linier** tanpa token awareness. Sistem akan mengalami context exhaustion setelah ~20-30 turns (estimated) atau ~80K tokens tergantung model. **Tidak ada mekanisme compression**, summarization, atau token tracking.

### Key Metrics:
- **Conversation Growth**: O(n²) tanpa compaction
- **Max Reliable Turns**: ~20-30 (hardcoded MAX_REQUESTS=16 + user turns)
- **Token Tracking**: ❌ None
- **Context Awareness**: ❌ None
- **Summarization**: ❌ None
- **Session Persistence**: ❌ None

---

## 1. AGENT LOOP DAN CONVERSATION HISTORY

### 1.1 Flow Diagram

```
User Input (CLI)
    ↓
StreamWorker._loop() [llm.py:174-216]
    ├─ Turn N: 0 to MAX_REQUESTS-1 (default 16)
    │
    ├─ stream_response(client, model, conversation)
    │   └─ OpenAI API call dengan full conversation
    │
    ├─ Replay response.output items ke conversation
    │   └─ conversation += [item.model_dump(...) for item in response.output]
    │
    ├─ Extract tool calls from response.output
    │   └─ calls = [item for item in response.output if item.type == "function_call"]
    │
    ├─ For each tool call:
    │   ├─ Execute run_tool() [tools.py]
    │   └─ add_tool_result(call_id, result) → conversation
    │
    └─ If calls exist → next turn
        Else → done

Conversation Storage: BaseAgent.conversation (list[dict])
```

### 1.2 Conversation Building

**First Turn** (`build_initial_conversation()` - agent_base.py:142-157):
```python
result = [
    {"type": "text", "role": "system", "content": SYSTEM_PROMPT},
    ...self.conversation  # User messages added via add_message()
]
```

**Each Turn** (llm.py:194):
```python
conversation += [
    item.model_dump(exclude_none=True) 
    for item in response.output
]
```

**Tool Results** (agent_base.py:124-128):
```python
self.conversation.append({
    "type": "function_call_output",
    "call_id": call_id,
    "output": result
})
```

---

## 2. STRUKTUR PESAN

### 2.1 Message Types

| Type | Role | Added By | Example |
|------|------|----------|---------|
| **System** | system | `build_initial_conversation()` | SYSTEM_PROMPT (13-25 llm.py) |
| **User** | user | `add_message("user", ...)` | "Read file X" |
| **Assistant** | assistant | `stream_response()` → API | Response text + reasoning |
| **Tool Result** | N/A | `add_tool_result()` | File contents, bash output |

### 2.2 Message Structure (OpenAI Format)

```python
# Text message
{
    "type": "text",
    "role": "system|user|assistant",
    "content": "..."
}

# Tool result
{
    "type": "function_call_output",
    "call_id": "call_xxx",
    "output": "..."
}
```

### 2.3 Conversation Growth

```
Turn 1: [sys] + [user1] + [asst1] + [tool_result1]      = N tokens
Turn 2: [prev] + [user2] + [asst2] + [tool_result2]    = prev + N tokens
Turn 3: [prev] + [user3] + [asst3] + [tool_result3]    = prev + N tokens
...
Turn 16: [all_previous] + [user16] + [asst16] + [tools]

Total ≈ N + 2N + 3N + ... + 16N = N × (1+2+...+16) = N × 136 tokens
Growth rate: O(n²) = ~O(136N) per MAX_REQUESTS turns
```

---

## 3. PENYIMPANAN SESSION DAN CONVERSATION HISTORY

### 3.1 Storage Mechanisms

| Component | Storage | Lifecycle | Persistence |
|-----------|---------|-----------|-------------|
| **Main Agent** | In-memory `StreamWorker.conversation` | Per session | ❌ Lost on exit |
| **Sub-Agents** | Per-agent `BaseAgent.conversation` | Per execution | ❌ Lost after completion |
| **File Changes** | `DiffEngine` snapshots | Per turn | ❌ In-memory only |
| **Delegation Stack** | `threading.local` | Per request thread | ❌ Thread-scoped |

### 3.2 Memory Management

**No session serialization:**
- Conversation only exists in Python memory
- No JSON/pickle export
- No resume capability
- No session file storage

**Delegation Tracking:**
```python
# delegation.py:22
_delegation_stack = threading.local()  # Per-thread context

class DelegationContext:
    depth: int      # Current nesting level
    count: int      # Total delegations in request
```

### 3.3 Per-Agent Isolation

```
Main Agent (StreamWorker)
├─ conversation: list[dict] = [msg1, msg2, ...]
│
└─ Delegated Sub-Agent 1
   ├─ conversation: list[dict] = []  (ISOLATED)
   ├─ execute() → own _loop()
   └─ Result → returned to main agent

```

---

## 4. KONFIGURASI MODEL DAN CONTEXT WINDOW

### 4.1 Model Configuration

**File**: `src/joy/llm.py` (lines 47-62)

```python
def load_config() -> dict:
    """Read connection settings from the environment."""
    model = (os.getenv("LLM_MODEL") or DEFAULT_MODEL).strip()
    return {
        "model": model.removeprefix("openai:"),
        "api_key": os.environ["OPENAI_API_KEY"],
        "base_url": os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
    }

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
```

**Env Variables:**
- `OPENAI_API_KEY` (required)
- `LLM_MODEL` (optional, defaults to gpt-5.6-luna)
- `OPENAI_BASE_URL` (optional, defaults to OpenAI)

### 4.2 Client Setup

```python
# llm.py:165-166
client = OpenAI(
    api_key=config["api_key"],
    base_url=config["base_url"],
    timeout=TIMEOUT,        # 60 seconds
    max_retries=0
)
```

### 4.3 Context Window Configuration

**STATUS: NOT IMPLEMENTED**

- No hardcoded model context windows
- No dynamic context limit checking
- No model-specific configuration
- Same behavior for all models (3.5, 4, 4-turbo, etc.)

**Related Constants:**
```python
MAX_REQUESTS = 16           # Iterations (hardcoded, not token-aware)
TIMEOUT = 60                # Wall-clock timeout only
MAX_OUTPUT_CHARS = 20_000   # Output truncation (NOT conversation)
```

---

## 5. TOKEN COUNTER DAN PENGHITUNGAN TOKEN

### 5.1 Current Status: NONE

**Evidence:**
```bash
$ grep -r "tiktoken\|token.*count\|encode.*token" src/
# Result: No matches
```

**No token tracking visible in:**
- `llm.py` (216 lines)
- `agent_base.py` (356 lines)
- `tools.py` (379 lines)
- `delegation.py` (455 lines)

### 5.2 Size Limits (Non-Token-Based)

```python
# tools.py
MAX_FILE_BYTES = 2 * 1024 * 1024      # 2 MB
MAX_OUTPUT_CHARS = 20_000              # Characters only
MAX_READ_LINES = 2000                  # Line count only

# llm.py
MAX_REQUESTS = 16                      # Iteration count
TIMEOUT = 60                           # Seconds

# agent_base.py
max_requests: int = 8                  # Per sub-agent
max_tool_calls: int = 4                # Per response
```

### 5.3 Conversation Growth Tracking

**No built-in metrics:**
- No token usage reported after each turn
- No context window percentage displayed
- No warnings before exhaustion
- No adaptive behavior

### 5.4 OpenAI SDK Usage Tracking

```python
# stream_response() - llm.py:103
stream = client.responses.create(
    model=model,
    input=conversation,           # ← Full conversation sent
    tools=response_tools(),
    reasoning={"effort": "medium"},
    include=["reasoning.encrypted_content"],
    store=False,
    stream=True,
)
```

**SDK may track tokens internally, but NOT exposed to Joy agent.**

---

## 6. PROVIDER LLM: PENGIRIMAN DAN PENERIMAAN PESAN

### 6.1 Request Format

**API Call Pattern** (llm.py:103-111):
```python
stream = client.responses.create(
    model=model,                    # "gpt-5.6-luna"
    input=conversation,             # list[dict] (full history)
    tools=response_tools(),         # Registered tools (read, write, etc.)
    reasoning={"effort": "medium"}, # Extended thinking
    include=["reasoning.encrypted_content"],
    store=False,                    # Don't store conversation
    stream=True,                    # Streaming response
)
```

**Conversation Format:**
```python
[
    {"type": "text", "role": "system", "content": "..."},
    {"type": "text", "role": "user", "content": "..."},
    {"type": "text", "role": "assistant", "content": "..."},
    {"type": "function_call_output", "call_id": "...", "output": "..."},
]
```

### 6.2 Response Processing

**Streaming Events** (llm.py:113-130):
```python
for event in stream:
    kind = getattr(event, "type", "")
    
    if kind == "response.output_text.delta":
        # Text chunk
        on_text(event.delta)
    
    elif kind == "response.refusal.delta":
        # Refusal message
        on_text(event.delta)
    
    elif kind == "response.completed":
        # Full response ready
        return event.response
    
    elif kind == "response.incomplete":
        # Timeout or incomplete
        raise LLMError(...)
```

### 6.3 Tool Call Execution

**Extraction** (llm.py:195):
```python
calls = [item for item in response.output 
         if item.type == "function_call"]
```

**Execution** (llm.py:200-216):
```python
for index, call in enumerate(calls):
    if total_tool_calls >= MAX_TOOL_CALLS:
        break
    
    name, args = call.function.name, call.function.arguments
    
    try:
        result = run_tool(name, **args)
        add_tool_result(call.id, result)
        total_tool_calls += 1
    except ToolError as exc:
        add_tool_result(call.id, f"Error: {exc}")
```

### 6.4 Full Message Lifecycle

```
1. User enters prompt
   ↓
2. StreamWorker.__init__(messages) with build_initial_conversation()
   ↓
3. _loop iteration:
   a. stream_response(client, model, conversation)
   b. For each event in stream:
      - Text delta → on_text() → UI display
      - Completed → get response object
   c. conversation += response.output items
   d. Extract tool calls
   e. Execute tools, add results to conversation
   f. If tools called → next iteration
      Else → done
   ↓
4. Return final conversation to CLI for display
```

---

## 7. MEKANISME SUMMARIZATION ATAU MEMORY

### 7.1 Current Status: NONE

**What exists:**
- ✅ `DiffEngine` (file change tracking)
- ✅ `DiffChangeManager` (change acceptance)
- ❌ Conversation compression
- ❌ Summarization module
- ❌ Memory hierarchy
- ❌ Selective retention

### 7.2 Diff System (Orthogonal)

**File**: `src/joy/diff_engine.py` (439 lines)

```python
class DiffEngine:
    """Track file changes, NOT conversation changes."""
    
    def capture_snapshot(self) → DiffSnapshot:
        """Snapshot files at point in time."""
    
    def compare_snapshots(...) → Dict[str, FileDiffMetadata]:
        """Compare before/after states."""
```

**This tracks:**
- ✅ File additions/deletions/modifications
- ✅ Line diffs
- ✅ File hashes

**This does NOT track:**
- ❌ Conversation history compression
- ❌ Message importance scoring
- ❌ Summary generation

### 7.3 Why No Summarization?

1. **Complexity**: Requires LLM to summarize, which consumes tokens
2. **Loss of Precision**: Summaries might lose critical context
3. **Trust**: User may need exact history for debugging
4. **Cost**: Additional API calls

**Current philosophy**: Keep full history as long as possible, truncate output only.

---

## 8. INTERAKSI MAIN AGENT, TOOL EXECUTION, SUB-AGENT

### 8.1 Architecture

```
┌──────────────────────────┐
│   Main Agent             │
│   (StreamWorker)         │
│   conversation: [...]    │
└────────────┬─────────────┘
             │
      ┌──────┴──────────────┐
      │                     │
      ▼                     ▼
   [Tools]              [Delegation]
   ├─ read_ide()        └─ delegate_ide()
   ├─ write_ide()          ├─ DelegationManager
   ├─ edit_ide()           ├─ SubAgentRunner
   ├─ bash_ide()           │  └─ conversation: [] (ISOLATED)
   └─ delegate_ide()       └─ Result → main agent
```

### 8.2 Tool Execution Flow

**File**: `src/joy/tools.py` (379 lines)

```python
def run_tool(name: str, **kwargs) -> str:
    """Execute registered tool and return result."""
    
    # Tools available:
    tools = {
        "read_ide": read_ide,
        "write_ide": write_ide,
        "edit_ide": edit_ide,
        "bash_ide": bash_ide,
        "delegate_ide": delegate,
    }
    
    try:
        result = tools[name](**kwargs)
        return result
    except ToolError as e:
        return f"Error: {e}"
```

**Tool Registration** (via aisuite):
```python
from aisuite.utils.tools import Tools

response_tools()  # Returns list of tool definitions
```

### 8.3 Sub-Agent Delegation

**File**: `src/joy/delegation.py` (455 lines)

**Flow:**
```
delegate_ide(task, context, ...)
  ↓
DelegationManager.execute_task(DelegationConfig)
  ↓
SubAgentRunner.from_config(AgentConfig)
  ├─ name: "sub_agent_1"
  ├─ system_instruction: "..."
  ├─ allowed_tools: ["read", "write", "edit", "bash"]
  ├─ max_requests: 8
  ├─ max_tool_calls: 4
  ├─ timeout_seconds: None
  │
  ├─ conversation: [] (ISOLATED)
  │
  └─ execute(task, llm_config)
     └─ Same agent loop as main agent
        ├─ Build initial conversation
        ├─ _loop() with own MAX_REQUESTS limit
        └─ Return AgentResult
           ├─ status: SUCCESS|FAILED|TIMEOUT
           ├─ output: "..."
           ├─ total_requests: N
           ├─ total_tool_calls: M
           └─ execution_time: T
```

### 8.4 Isolation & Constraints

**Per Sub-Agent:**
```python
@dataclass
class DelegationConfig:
    task: str                           # Task to delegate
    system_instruction: Optional[str]   # Custom system prompt
    allowed_tools: list[str]            # Tool whitelist
    context: Optional[str]              # Additional context
    max_requests: int = 8               # Limit iterations
    max_tool_calls: int = 4             # Limit tool calls
    timeout_seconds: Optional[float]    # Wall-clock timeout
    max_delegation_depth: int = 2       # Recursion limit
    max_delegations_per_request: int = 3 # Rate limit
```

**Depth Tracking** (delegation.py:151-184):
```python
class DelegationContext:
    depth: int = 0                      # Current nesting
    count: int = 0                      # Total delegations
    
    @staticmethod
    def get_current() → DelegationContext:
        # Per-thread context
```

### 8.5 Parallel Execution (Planned)

**File**: `src/joy/parallel_execution.py` (477 lines)

**Features:**
- ✅ TaskPool (concurrent execution)
- ✅ Task dependencies (not used)
- ✅ File access management
- ✅ Timeout & cancellation
- ❌ Not integrated into main flow

```python
class TaskPool:
    """Manage concurrent sub-agent tasks."""
    
    def __init__(self, max_concurrent: int = 4):
        self.max_concurrent = max_concurrent  # 1-16
    
    def submit_task(config: TaskConfig) → str:
        """Queue task, return task_id."""
    
    def wait_for_all(timeout: float = None) → Dict[str, TaskResult]:
        """Wait for all tasks, return results."""
```

---

## 9. TEST SUITE CONDITION

### 9.1 Test Files Exist

```
tests/
├── test_agent_base.py              (409 lines) ✅
├── test_agent_delegation_flow.py   ✅
├── test_cli_delegation.py          ✅
├── test_parallel_execution.py      ✅
├── test_tools.py                   ✅
├── test_diff_engine.py             ✅
├── test_diff_ui.py                 ✅
├── test_diff_integration.py        ✅
├── test_diff_change_management.py  ✅
└── test_change_management_integration.py ✅
```

### 9.2 Test Coverage

**Unit Tests:**
- Agent lifecycle (creation, execution, status)
- Message building (system, user, assistant, tool results)
- Tool execution (mocked)
- Sub-agent isolation

**Integration Tests:**
- Delegation flow (main → sub-agent)
- Tool execution in real environment
- File changes with diff tracking

**NOT COVERED:**
- Token counting (no implementation)
- Long sessions (>20 turns)
- Context exhaustion
- Session recovery

### 9.3 Running Tests

```bash
# All tests
pytest tests/ -v

# Specific test
pytest tests/test_agent_base.py -v

# With coverage
pytest --cov=src/joy tests/
```

---

## SUMMARY TABLE

| Aspect | Status | Details |
|--------|--------|---------|
| **Agent Loop** | ✅ Implemented | StreamWorker._loop(), max 16 iterations |
| **Conversation History** | ✅ Stored | In-memory `list[dict]`, per-agent |
| **Message Structure** | ✅ Compliant | OpenAI API format |
| **Tool Execution** | ✅ Implemented | read, write, edit, bash, delegate |
| **Sub-Agent Isolation** | ✅ Good | Separate conversations per agent |
| **Token Counting** | ❌ Missing | No tiktoken integration |
| **Context Awareness** | ❌ Missing | No model-specific limits |
| **Summarization** | ❌ Missing | No compression strategy |
| **Session Persistence** | ❌ Missing | No serialization |
| **Long Sessions** | ⚠️ Limited | ~20-30 turns before exhaustion |
| **Test Coverage** | ✅ Decent | Agent + delegation tests exist |

---

## RECOMMENDATIONS FOR CONTEXT COMPACTION

### Phase 1: Token Awareness (CRITICAL)
1. Add `tiktoken` dependency
2. Implement `TokenCounter` class
3. Track tokens per turn
4. Add model context windows

### Phase 2: Compression Strategies
1. Implement pruning (remove low-value messages)
2. Implement summarization (LLM-based)
3. Implement hierarchical (summary + recent context)
4. Add configuration for strategy selection

### Phase 3: Monitoring
1. Real-time context usage display
2. Compression metrics logging
3. Warnings before exhaustion
4. Turn estimation remaining

### Phase 4: Integration
1. Integrate with StreamWorker
2. Integrate with SubAgentRunner
3. Update delegation system
4. Add end-to-end tests

### Phase 5: Polish
1. Session persistence (optional)
2. Performance optimization
3. Documentation & examples
4. Migration guide

---

## REFERENCES

| File | Lines | Purpose |
|------|-------|---------|
| llm.py | 216 | Streaming API + agent loop |
| agent_base.py | 356 | Base agent abstraction |
| tools.py | 379 | Tool registration & execution |
| delegation.py | 455 | Sub-agent delegation |
| cli.py | 376 | Terminal interface |
| diff_engine.py | 439 | File change tracking |
| parallel_execution.py | 477 | Concurrent task execution |

**Total codebase**: ~2,700 Python lines (excluding tests and diff system)

---

## NEXT STEPS

1. **Confirm** findings with team
2. **Design** token counter API
3. **Implement** Phase 1 (token awareness)
4. **Test** with long session scenarios
5. **Iterate** based on real-world usage

