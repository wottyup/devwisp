# Android live turn timer alignment

## Requirement and classification

The running reply's “已用” timer on Android leads the web timer by several seconds, sometimes more than ten. The user confirmed that the issue concerns the running timer. Fix the shared native path used by Codex, Claude and DeepSeek.

This is a PATCH correction to the existing elapsed-time display. Target Android `0.133.3-dev.1`, versionCode `488`. Preserve the installed application/signing identity, existing APKs and unrelated server/web edits. Commit locally without pushing.

## Findings

- The tick already measures monotonic elapsed time; it does not accumulate seconds faster than real time.
- The initial elapsed value subtracts a server timestamp from the phone wall clock, so phone clock skew appears in the timer. An unauthenticated local API response confirms that the existing HTTP `Date` header is available through the web proxy. No server change is necessary.
- A `turn/started` event renders before new history arrives; the current implementation can read the preceding turn's start. Start metadata arriving later for the same turn never corrects its initial timer.
- Android's start timestamp aliases and fallback order differ from `src/lib/turnTiming.ts`.

## Implementation steps

1. Extract a testable timing model that matches the web's first-valid start timestamp and latest user-message fallback. Select the pending live turn rather than borrowing a previous turn while history catches up.
2. Learn server time from quick, uncached HTTP responses, scoped per endpoint. Bound retained state, reject slow/invalid samples, prefer low latency samples within a refresh window and advance estimates with monotonic time. HTTP dates have one-second precision; this is not millisecond synchronization. Retain usable monotonic fallback behavior when no date is available.
3. Observe live starts before rendering; allow authoritative history to correct the same turn. Keep bounded per-turn state scoped by endpoint, engine, thread and turn across rerenders/thread switches. Continue displaying seconds from actual elapsed time, including across background pauses.
4. Add regression tests for clock skew, missing/delayed/corrected timestamps, back-to-back turns, source aliases/units/fractions, endpoint isolation and missing/slow clock samples. Increment the patch version and add a concise Android changelog summary.
5. Build and run Android unit tests offline. Verify APK metadata/signature; archive a new file exclusively and verify the preceding APK is unchanged. Exercise update selection and full download using isolated fixture authentication, without operating real conversations. Record phone-validation limits, remove temporary logs and commit only this task's files.

## Validation record

- The running timer now observes `turn/started` before rendering and selects the pending turn while history catches up. It retains separate timing state per endpoint/engine/thread/turn, accepts later timestamp corrections and preserves monotonic fallback starts across rerenders and thread switches. Start alias priority, numeric seconds/milliseconds (including fractional seconds), ISO offsets and latest-user fallback follow the web helper.
- `RemoteApi` learns an endpoint-scoped monotonic server clock from successful, uncached responses with a valid HTTP date. Samples over two seconds, malformed/missing dates and out-of-order observations are ignored. Lower-latency samples are preferred for 60 seconds, then refreshed. Cached estimates survive responses without dates; before calibration the existing local-time assumption remains a fallback. HTTP date precision and network latency can leave a small residual difference; exact phone/web equality has not been measured.
- Offline `:app:assembleDebug :app:testDebugUnitTest` succeeded in 1m 32s. All 115 tests in 15 suites passed, with zero failures/errors/skips, including 14 new regression tests for timestamp selection, current-turn identity, delayed history, phone clock offsets of ±15 seconds, monotonic fallback/background pauses, endpoint/engine/thread isolation and server-clock sampling.
- APK metadata: package `com.sevencnup.codexremote`, version `0.133.3-dev.1`, versionCode `488`, minSdk 26 / targetSdk 35. Signature verified; signer SHA-256 remains `5201a7a330e862cd428823c3b7482bd15d351f45e2debdd246121df5bc0eced1`.
- Exclusively archived `artifacts/devwisp-android-0.133.3-dev.1-debug.apk`, 7,669,862 bytes, SHA-256 `83591983330d1e726c77e100e64d3e72ad233db267348b36ec46fd1f34b9d1b7`. The preceding `0.133.2-dev.1` archive checksum is unchanged.
- Actual Vite update middleware passed isolated fixture verification: unauthenticated requests rejected, latest-version metadata/size/hash correct and the full authenticated APK download byte-identical to the archive. Fixture authentication did not use real pairing credentials; its loopback server was closed afterward.
- No ADB device is attached. A side-by-side phone/web timer comparison remains unverified; no real conversations or terminals were operated and no application was installed. No dependencies, service restart or remote push were needed. The temporary build log was removed before the local commit; unrelated ongoing server/web work was preserved.
