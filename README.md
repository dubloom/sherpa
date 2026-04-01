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

Only claude model are supported for now.

The review will contain four potential categories of feedback:
- High issues, considered as errors, they will block the commit
- Medium issues, considered as warnings, they will not block the commit
- Low issues, considered as debug, they will not block the commit
- Nice to have.

Each issue/nit will be marked with an identifier than can be used later for the fixing stage.

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