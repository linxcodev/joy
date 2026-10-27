# Joy

A terminal coding agent built with Python, the OpenAI SDK, aisuite, prompt_toolkit, and Rich.

Joy streams replies from the OpenAI Responses API (`reasoning.effort` set to
`medium`) and can use four tools in the directory where you launch it. The
conversation is kept in memory for the session only.

## Tools

| Tool    | Arguments                          | Behavior                                                     |
| ------- | ---------------------------------- | ------------------------------------------------------------ |
| `read`  | `path`, `offset=1`, `limit=200`    | Returns numbered UTF-8 lines, 1-based, up to 2,000 per call   |
| `write` | `path`, `content`                  | Creates or overwrites a file and creates parent directories   |
| `edit`  | `path`, `old_text`, `new_text`     | Replaces one unique match and returns a unified diff          |
| `bash`  | `command`, `timeout=30`            | Runs Bash for 1–300 s and returns output and the exit code    |

- File paths are resolved against the launch directory. Paths and symlinks
  that point outside it are rejected. Reads and writes are limited to 2 MB.
  Tool output is cut off at 20,000 characters, with a marker.
- `edit` tries an exact match first. Otherwise it compares text after NFKC
  normalization, trailing-whitespace stripping, and folding of smart quotes,
  dashes, and special spaces. Ambiguous, missing, or no-op edits are rejected.
  Edits keep the BOM, the LF/CRLF line endings, permission bits, and untouched
  lines. Saves are atomic and fail if the file changed in the meantime.
- **`bash` runs with your user permissions and is not sandboxed.** The path
  checks only apply to the file tools. On timeout or Ctrl+C, the command's
  whole process group is terminated.

aisuite's `Tools` helper builds the schemas, validates arguments, and runs the
tools. Tool errors are returned to the model so it can recover. A turn can make
at most 16 model requests and run at most 8 tool calls per response.

## Install

Requires Python 3.10–3.13.

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .   # pulls in openai and aisuite
```

## Configure

Joy reads its settings from the environment:

| Variable          | Required | Purpose                                   |
| ----------------- | -------- | ----------------------------------------- |
| `OPENAI_API_KEY`  | yes      | API key (never printed or stored by Joy)  |
| `LLM_MODEL`       | no       | Defaults to `gpt-5.6-luna`                |
| `OPENAI_BASE_URL` | no       | Defaults to `https://api.openai.com/v1`   |

The endpoint must support the Responses API, including function tools, and
reasoning items with `store=false`.

If the key is missing, Joy shows a setup message at startup and on
each send.

## Run

```sh
joy
```

## Usage

- **Enter**: send a message. While a reply is streaming, Enter does nothing.
- **Alt+Enter**: insert a newline (the input stays one row tall).
- **Ctrl+C**: clear the current input, or stop the streaming reply.
- **Ctrl+D** on empty input, or **/exit**: exit.

The input stays editable while a reply streams, and anything you type is kept as
your next draft. While a tool runs, a compact line shows its name and status
(running, finished, or failed). When the reply finishes, it is printed once as
Markdown.

Ctrl+C and API errors keep any partial reply on screen with a message. API
errors show only the status, code, and parameter. Tools run only after a
response completes successfully. Failed or cancelled turns are left out of
history. The exception is a turn that already used tools: Joy keeps the request
with a short interruption note, so a retry checks for existing changes.
Cancelling stops further tool calls, but completed writes are not undone.

Requests time out after 60 seconds and are not retried automatically. Each
turn runs in a background thread. Ctrl+C stops the display right away, but a
blocked HTTP read can keep going until the next event or the timeout. Its
output is then thrown away.

## Test

HTTP streams are mocked and tools run in temporary directories, so no API key is needed:

```sh
python -m unittest discover -s tests
```

## Next steps

See `docs/prompts/` for the tutorial stages.
