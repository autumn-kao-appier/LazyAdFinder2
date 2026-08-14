# AOS Settings Navigation Follow-up

Status: deferred until an Android device is available.

## Problem

Android Settings navigation is inherently sensitive to Android version, Google
Play services, locale, OEM UI, animation timing, and selector changes. A single
navigation or screenshot failure can currently remove the expected Evidence for
multiple TestCases and amplify an infrastructure problem into misleading test
results.

The separate verdict issue has already been addressed: missing or unreadable AOS
Evidence must produce `BLOCKED`, not `FAILED`. This note tracks the remaining
navigation and failure-isolation work only.

## Direction agreed so far

- Do not aim for a guarantee that Settings UI navigation always succeeds.
- Reduce the blast radius when it fails.
- Prefer independent ADB/system facts for machine comparison when they are a
  valid source of truth.
- Keep Settings screenshots as visual Evidence where appropriate.
- A failed Evidence provider should affect only the TestCases that depend on it,
  not abort or invalidate an otherwise usable Scenario capture.
- GAID/tracking needs separate consideration because the user-visible Settings
  state may itself be part of the contract and SDK payload cannot be used to
  prove its own correctness.

No Settings automation changes should be made from this note without first
testing the current behavior on a real Android device.

## Policy questions to resolve

1. For each TestCase, is the Settings screenshot required independent truth or
   supporting visual Evidence?
2. Which ADB/system sources are sufficiently independent to replace Settings UI
   as the expected value?
3. When machine truth is captured but the screenshot fails, should the TestCase
   complete comparison or remain `BLOCKED` because the visual contract is
   incomplete?
4. Which GAID/tracking states must be proven visually?
5. What Android devices, versions, locales, and OEM Settings variants are in the
   supported automation matrix?

## Device-session investigation checklist

- Record device model, Android version, build fingerprint, locale, resolution,
  font scale, Google Play services version, and current Settings activity.
- Run every AOS Settings Evidence capture individually before running a suite.
- For each navigation step, preserve:
  - activity/package before and after launch;
  - UI hierarchy XML;
  - screenshot on success and failure;
  - locator strategy attempted;
  - elapsed time and retry count.
- Verify which pages support stable direct intents.
- Compare resource IDs, accessibility descriptions, visible text, and structural
  selectors; treat coordinates as a last-resort device-specific fallback.
- Test recovery from an unexpected page, screen lock, animation delay, and a
  previously open Settings task.
- Confirm that one provider failure can be recorded in `evidence-errors.json`
  without preventing unrelated providers and validators from completing.
- Exercise GAID/tracking allowed and denied flows separately.

## Candidate implementation after device findings

- Introduce a Settings navigator with explicit page recognition, bounded retry,
  and recovery to a known state.
- Change Evidence collection from fail-fast to per-provider results.
- Map each TestCase to its required Evidence so provider failures become local
  `BLOCKED` verdicts.
- Use ADB/system truth for fields where it is valid; retain visual artifacts as
  supporting Evidence or a required visual contract according to the policy
  decisions above.
- Add regression fixtures from the captured UI hierarchies before changing
  selectors.

## Completion criteria

- A Settings navigation failure does not create a false `FAILED` verdict.
- Unrelated TestCases still complete after one Evidence provider fails.
- Every `BLOCKED` verdict names the failed artifact/provider and preserves useful
  diagnostics.
- Evidence source requirements are explicit per TestCase.
- The supported device matrix and known unsupported variants are documented.
