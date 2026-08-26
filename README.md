# codex-pilot

A Claude Code plugin for driving [Codex](https://openai.com/index/codex/) Desktop
threads: send work, steer a turn that is already running, stop one, answer its
approval requests, change model / reasoning / plan mode, and get told when a
thread goes idle.

Useful if you supervise several Codex agents at once and would rather orchestrate
them from Claude Code than tab between them.

```sh
/plugin marketplace add thomast8/codex-pilot
/plugin install codex-pilot@codex-pilot
```

Requires macOS, [uv](https://docs.astral.sh/uv/), and Codex Desktop.

Full documentation, the eleven tools, and the decoded protocol are in
[`plugins/codex-pilot/README.md`](plugins/codex-pilot/README.md) and
[`plugins/codex-pilot/docs/protocol.md`](plugins/codex-pilot/docs/protocol.md).

## How it works, briefly

Codex Desktop's Electron process runs a local IPC router on a unix socket that its
own windows use to drive threads. This plugin joins that bus as a client and
speaks the same protocol — decoded from the installed app bundle and validated
against a running app. It never bypasses Codex's single-writer lock: a thread the
app has open is driven through the app, and one nothing holds is resumed with
`codex exec resume` instead.

The protocol is private and undocumented, so a Codex Desktop update can change it.
`scripts/extract_registry.py --check` diffs every installed app bundle against the
pinned protocol version map, and the test suite runs the same check.

> Written with Claude (Opus 5), driven and reviewed by Thomas Tiotto. Every
> protocol claim marked *verified* in the docs was executed against a live Codex
> Desktop; anything decoded-but-unexecuted is labelled as such.

## License

MIT
