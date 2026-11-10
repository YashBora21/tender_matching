# Release candidate — 2026-11-10

Tag: `rc-2026-11-10.1`  
Tested source commit: `db15dab91ff9904478d0526ae26e2d6bdbeb4b4c`

The consolidated offline unit suite passed all 1,128 tests in 59.333 seconds.
One earlier attempt was stopped after the Windows sandbox blocked TestClient local sockets; the successful attempt used synthetic provider settings and offline model loading. No paid API calls, schema migration or business-data refresh occurred.

Saved-response replay checked twelve rows, preserved raw source quotations, canonicalized six mappings and abstained on six unmapped attributes. One value and one clause type changed. This does not establish full-tender matching accuracy or processing time.

The user will identify the intended pump file. The held pump proposals are unconfirmed. Full-tender labels must be confirmed before the valve/pump acceptance run. Grouping remains off. Refresh must retain historical revisions and results; source contains the additive history migration but it has not been applied in this phase.

The Docker Hub guide contains exact build, privacy, smoke, push and anonymous visibility checks. No images were built or published. Container Paddle/CUDA compatibility remains untested.

The RC tag points to the tested source; a subsequent audit-only commit records this manifest without changing that tag. Local scratch/corpus/large historical outputs are intentionally excluded from the source commits. Use a clean checkout of the tag for release builds.
