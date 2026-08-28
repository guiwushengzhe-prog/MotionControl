# MotionControl v0.9.7-dev handoff — 2026-08-25

## Baseline

- PC: reconstructed from the complete 2026-08-24 source tree, then overlaid file-for-file with the 2026-08-25 `MotionControl-v0.9.6-CHATGPT-FINAL` manifest before v0.9.7 work.
- Mobile: 2026-08-25 v0.9.6 manifest files were overlaid before adding `control_config_v1` display/cache support. Current dev version is `0.9.7-dev`, Android `versionCode 23`.
- Do not restart from the older 0.9.5 complete ZIP. It is only a completeness donor for files not present in the v0.9.6 transport package.

## Implemented

1. Offline lazy-loaded Game Profile v1 store and catalog.
2. Unified output action schema and runtime execution for keyboard, mouse buttons/wheel, Xbox buttons/triggers/left-stick directions.
3. Search/select Profile APIs and PC mapping UI.
4. Existing six Zones remain compatible with Y/X/B/A/LB/RB defaults.
5. Crossed arms + two leg-cross poses, edge-triggered once.
6. Existing right-wrist vertical view behavior preserved; head pitch remains excluded from output Y.
7. `control_config_v1` broadcast from PC and read-only cached display on both mobile roles.
8. Steam Input VDF parser supports Valve v2 and v3 `inputs -> activators -> Full_Press -> bindings`, preset/group source resolution, key/mouse/wheel/xinput outputs. Long/double-press activators are not silently flattened into normal Zone actions.
9. SteamInputDB builder: official configs first, then ranked community configs; deterministic conversion only, no AI control guessing.
10. Incremental library checkpoints, existing-good skip/resume, retry failures; no global minimum Profile build gate.
11. Steam Top Sellers Windows seed collector with fixed priority games and deduplication.
12. Library audit + persistent human verification metadata.
13. One-click Windows scripts: `BUILD_GAME_LIBRARY.bat`, `BUILD_PRIORITY_PROFILES.bat`.

## Verification status

- PC Python full regression at this handoff: `113 passed, 19 skipped, 0 failed`.
- New Profile/audit/verification focused tests are included under `tests/*_v097.py`.
- Mobile new TypeScript source was checked with TypeScript 5.8.3 using temporary declarations only for missing third-party global type packages in this restricted environment; no source type error was found.
- Full `npm ci` could not complete in the current restricted network environment.
- Android APK compile is not claimed complete here because the source snapshot does not include `gradle-wrapper.jar` and this environment has no global Gradle. Run it in the normal local Android toolchain.

## Next work

1. On the real local machine with network, run `BUILD_PRIORITY_PROFILES.bat` first and inspect `game_profiles/audit_report.json`.
2. Run `BUILD_GAME_LIBRARY.bat` repeatedly to grow the offline library; Profile count is a progress metric, not a gate.
3. Human-test priority games and record successful verification with `tools/mark_profile_verified.py`.
4. Build/sync Android in the normal local environment and verify the mobile current-game/Zone display during PC Profile switches.
5. Only after real game tests, tune individual Profile mappings or add per-game overrides; do not infer missing game controls with an LLM.
