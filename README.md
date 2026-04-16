# sherpa

`sherpa` is a CLI tool allowing you to make an AI review locally your changes before comitting.

## Install on your machine

```bash
pipx install .
```

## Usage

### Review before committing

In order to have an AI review your **staged** changes before comitting you can use:

```bash
sherpa commit -m "my commit message"
```

`sherpa commit` is intended to be use exactly like `git commit`. Specifically, all arguments
passed after `sherpa commit` are forwared to `git commit`.

You can specify the model you want to use for your review using the `--model` flag:

```bash
sherpa commit --model claude-opus-4-5 -m "my commit message"
```

If `--model` is not provided, sherpa uses `.sherpa/config.json`.
On first launch, if no config is found, sherpa starts an interactive setup to choose:
- your default model (from the supported models list)
- your default reasoning effort (`low`, `medium`, `high`)

Then it creates `.sherpa/config.json`:

```json
{
  "default_model": "gpt-5.3-codex",
  "default_reasoning_effort": "medium"
}
```

`default_reasoning_effort` controls the review reasoning effort (OpenAI models only) and must be one of:
`low`, `medium`, or `high`.

The review will contain four potential categories of feedback:
- High issues, considered as errors, they will block the commit
- Medium issues, considered as warnings, they will not block the commit
- Low issues, considered as debug, they will not block the commit
- Nice to have.

Each issue/nit will be marked with an identifier than can be used later for the fixing stage.

### Fix selected issues

Use `sherpa fix` to select one or more issues from the latest stored review and apply fixes.

```bash
sherpa fix
```

You can target specific issue IDs:

```bash
sherpa fix H0 M1
```

Before each selected fix task starts, Sherpa prompts:

```text
Extra Instruction:
```

Leave it blank to run without extra instructions.

### Review

If you want to review an existing commit, you can use

```bash
sherpa review <commit>
```

sherpa has two flags to allow you to quickly review the latest commit or the branch against main or master:

```bash
sherpa review --last
sherpa review --branch
sherpa --model claude-haiku-4-5 review --branch
```