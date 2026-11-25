# Mirage — Complete Implementation Plan

**Status:** Implementation handoff 1.0  
**Scope:** Standalone local image-generation application built around `stable-diffusion.cpp` (`sd.cpp`)  
**Initial target:** Linux desktop (ALT Linux), AMD Radeon 780M (RADV/Vulkan), 32 GB shared RAM, rootless Podman + podman-compose  
**Interfaces:** Python CLI, JSON HTTP API, buildless local web UI  
**Out of scope for v1:** Sezam integration, multi-user cloud deployment, training/fine-tuning, custom inference kernels

## 1. Goal and non-negotiable requirements

Build a reproducible application that sets up and manages `sd.cpp`, downloads compatible model component files, performs text-to-image generation and supported reference-image editing, and maintains a durable searchable gallery. A CLI and browser GUI must operate on **one backend, one job queue, one model registry, and one SQLite database**.

Must-haves:

1. Bootstrap the environment from an empty application data directory, with clear preflight checks and actionable errors.
2. Build `sd.cpp` in an isolated **builder container**, export binaries and all required shared libraries into **persistent host storage**, and run them from a separate lightweight **runtime container** via read-only bind mounts. Deleting container images must **not** require recompiling `sd.cpp`.
3. Install MOSS-Image-SFT, Qwen-Image 2.1, and Z-Image Turbo with their required LLM GGUF, tokenizer, VAE and optional Qwen vision mmproj; verify downloads and permit resuming.
4. Keep image size presets and model-specific quality/generation presets in a persistent editable JSON file. CLI, API and GUI must consume the same effective configuration and support safe reload without rebuilding or touching source code.
5. Expose Python CLI and a pleasant **buildless** local web GUI: static HTML, CSS, native ES modules; **no Node.js/npm/Vite/webpack/React/Vue build pipeline**. A tiny pinned vendored JS library is optional but should not be necessary.
6. Provide automatic system/light/dark themes and automatic browser-locale English/Russian localization, each with a persistent manual override.
7. Provide persistent asynchronous generation queue, progress/events, cancellation, retry, durable logs, metadata and browsable gallery. Closing the browser must not terminate jobs.
8. Preserve the verified desktop-friendly Qwen editing CPU/Vulkan backend split.
9. Safe-by-default local access, filesystem path validation, no output overwrites, and explicit deletions.

**Implementation priority:** correctness, recoverability, modest complexity and preservation of the tested inference workflow before visual polish.

## 2. Target software architecture

```text
                             Browser GUI (static files)
                                    |
                           JSON REST + SSE
                                    |
CLI (mirage) ----> Common Python application services
                                    |
                   +----------------+----------------+
                   |                |                |
             Model Manager    Job Scheduler    Gallery/Media
                   |                |                |
               Downloads       SQLite queue      SQLite + files
                                    |
                          sd.cpp process runner
                                    |
                      Podman/Docker runtime container
                                    |
                          /opt/sd/bin (host mount)
                                    |
                          Vulkan GPU + CPU backend

Builder container --exports--> host data/runtimes/releases/<revision>/bin
```

**Separation of responsibilities:**
- Python owns manifests, configuration, downloads, build artifacts, processes, queues, persistence, security and API.
- Browser owns presentation only. It never constructs sd.cpp arguments, reads host filesystem paths, or maintains authoritative job state.
- CLI invokes the same application services (or the local service API for queued operations), and cannot bypass validation.
- Inference uses a supervised one-shot `sd-cli` process per job in v1, not a permanently resident model. Preserve `sd-server` as a future optional adapter, not a prerequisite.

## 3. Layout and persistent storage

Recommended source tree:

```text
mirage/
  README.md
  pyproject.toml
  compose.yaml
  config/
    models.json                  # shipped model/component manifest
    generation-presets.default.json
    settings.default.json
  docker/
    Dockerfile.builder
    Dockerfile.runtime
    Dockerfile.app                # optional app container
  scripts/bootstrap.sh
  src/mirage/
    cli.py api.py config.py manifest.py downloader.py
    runtime_builder.py runtime_manager.py runner.py queue.py
    database.py gallery.py images.py progress.py security.py
  web/
    index.html
    css/{tokens,layout,components,theme,gallery}.css
    js/{app,api,router,state,i18n,theme,generate,gallery,jobs,models,settings}.js
    locales/{en,ru}.json
    icons/icons.svg
    vendor/                       # only if needed; pinned offline assets
  tests/
  data/                           # host bind mount; NEVER in git
    config/
      generation-presets.json
      settings.json
    runtimes/
      releases/<version>/bin/
      releases/<version>/manifest.json
      current -> releases/<version>
    models/{moss,qwen,zimage}/
    downloads/
    inputs/
    outputs/
    thumbnails/
    logs/
    mirage.sqlite3
```

The repository may be upgraded independently of `data/`. All state-bearing directories must be bind-mounted from the host, not stored only in containers or images. Document the exact mount points. Never delete user data in setup/repair/update commands. Container cleanup does not protect against intentional deletion of `data/` itself.

## 4. Models and compatibility manifest

Model IDs must remain stable and language-independent:

| ID | Model | Diffusion | Text encoder | Other components | Tasks |
|---|---|---|---|---|---|
| `moss` | MOSS-Image-SFT | `MOSS-Image-SFT-Q4_K.gguf` | `Qwen3.5-9B-Q4_K_M.gguf` | `flux2-vae_small.safetensors`, `tokenizer.json` | txt2img |
| `qwen` | Qwen-Image 2.1 | `qwen_image_2.1-Q4_K.gguf` | `Qwen3VL-8B-Instruct-Q4_K_M.gguf` | `qwen_image_2.1_vae_bf16.safetensors`; optional `mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf` | txt2img, edit (with mmproj) |
| `zimage` | Z-Image Turbo | `z_image_turbo-Q4_K.gguf` | `Qwen3-4B-Instruct-2507-Q4_K_M.gguf` | `ae.safetensors` | txt2img |

Store precise Hugging Face repositories, exact relative paths, required/optional flag, local path, expected bytes/checksum (when verified), license notice, compatibility rules and upstream sd.cpp minimum revision in `config/models.json`. **Before implementation, verify each URL and filename against its live repository**. Treat prior exploratory filenames as manifest candidates, not a substitute for current remote verification. Use known working local components when available.

Relevant upstream sources:
- https://github.com/leejet/stable-diffusion.cpp
- https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/models/moss_image.md
- https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/models/qwen_image_2.1.md
- https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/models/z_image.md
- https://huggingface.co/leejet/MOSS-Image-SFT-GGUF
- https://huggingface.co/leejet/Qwen-Image-2.1-GGUF
- https://huggingface.co/leejet/Z-Image-Turbo-GGUF
- https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF

`qwen` reference editing requires the **matching vision projector** and `--llm_vision`. `--ref` is repeatable for multiple references. Reject reference images for `moss` and `zimage` v1 with a clear capability error. Do not assume the models' Qwen-family text encoders are interchangeable.

## 5. External editable generation presets (mandatory)

The **effective** configuration lives at `data/config/generation-presets.json`; ship initial defaults at `config/generation-presets.default.json`. Copy defaults only when no effective file exists. Never overwrite effective configuration on upgrade.

Exact initial defaults:

```json
{
  "schema_version": 1,
  "defaults": {"model": "zimage", "resolution": "hd", "quality": "high"},
  "resolutions": {
    "sd": {"width": 512, "height": 512},
    "hd": {"width": 1024, "height": 1024},
    "fhd": {"width": 1920, "height": 1080}
  },
  "models": {
    "moss": {
      "default_quality": "medium", "cfg_scale": 3.0,
      "sampler": "euler", "scheduler": "simple",
      "quality": {"fast": {"steps": 20}, "medium": {"steps": 35}, "high": {"steps": 50}}
    },
    "qwen": {
      "default_quality": "medium", "cfg_scale": 6.0,
      "sampler": "euler", "scheduler": "simple",
      "quality": {"fast": {"steps": 15}, "medium": {"steps": 25}, "high": {"steps": 40}}
    },
    "zimage": {
      "default_quality": "high", "cfg_scale": 1.0,
      "sampler": "euler", "scheduler": "simple",
      "quality": {"fast": {"steps": 4}, "medium": {"steps": 6}, "high": {"steps": 8}}
    }
  }
}
```

Quality settings are model-specific; resolution settings are application-wide. Preset IDs (`fast`, `medium`, `high`, `sd`, `hd`, `fhd`) are stable API identifiers and are translated only in the UI. Resolution and step count are independent. Fresh requests reject width and height overrides. Values represent tested/working initial presets, **not proven perceptual quality equivalence**.

Requirements:
1. CLI, API and GUI read one validated effective configuration via the Python configuration service. No duplicate numerical tables in frontend JS.
2. Support `mirage config show|validate|reload|restore-defaults` and API `GET /api/config/generation`, `PUT /api/config/generation`, `POST /api/config/generation/reload`, `POST /api/config/generation/restore-defaults`.
3. A reload parses and validates **the entire document**, then atomically swaps a versioned immutable configuration snapshot. Invalid JSON or values leave the prior snapshot active, with an actionable error.
4. File edits are applied by explicit reload; optional polling/file-watch support may be added later. Updates through API use atomic temporary-write + fsync + rename.
5. Validate known model IDs, width/height positive and supported divisibility, supported samplers/schedulers for pinned runtime, steps bounded sensibly, CFG finite/nonnegative, default keys present, no duplicate IDs.
6. Capture the fully resolved effective arguments (and configuration revision/hash) **when the job is submitted**, not when the worker starts. Queued/running jobs never change if presets are edited.
7. Per-job overrides are allowed but do not modify global configuration.
8. Preserve config on upgrades; supply explicit migration for schema changes, with backups and rollback.
9. Edit through GUI Settings with validation, save, reload, restore and warning confirmation for restoring defaults. Admin-only operations if LAN access/authentication is enabled.

Separate `models.json` (component identity/dependencies), `generation-presets.json` (user-tunable generation settings), and `settings.json` (paths, concurrency, optional backend policies). User language/theme preferences are **browser-local**, not system-wide.

## 6. Backend defaults and hardware-friendly behavior

Preserve existing known-good options unless benchmarking justifies change:
- Default sampler Euler, scheduler Simple.
- `--offload-to-cpu` supported.
- For Qwen image editing with reference inputs, default to:

```text
--backend te=cpu,clip_vision=cpu,diffusion=vulkan0,vae=vulkan0
--offload-to-cpu
```

Measured POC: CPU conditioning and projector + Vulkan diffusion/VAE reduced GPU-visible use considerably and restored **single-segment** diffusion. Make this a named backend profile in configuration/registry; do not hardcode it only in the CLI.

Record in job logs the actual backend flags, `sd.cpp` version, text conditioning time, number of diffusion segments, step timing, VAE time and complete wall clock. Treat `--max-vram` as an optional advanced tuning knob, **not** a hard guarantee of total GPU memory use. Default to **one active GPU inference job**.

## 7. Installation and build-artifact lifecycle

### 7.1 Preflight

`mirage setup` / `scripts/bootstrap.sh` must check: Python 3 version, container runtime and compose provider, access to `/dev/dri` render device, rootless Podman group inheritance (`group_add: [keep-groups]` when applicable), Vulkan device discovery, free disk space, write access, and existing artifacts. Provide exact remediation when checks fail. No assumptions that a Linux group literally named `render` exists.

### 7.2 Build image

Pin sd.cpp to a specific **tested commit**, including submodules; do not silently build moving `master`. Derive CMake flags from the **verified upstream Dockerfile/build docs for that pinned revision**; enable Vulkan and build `sd-cli`, `sd-server` and GGML backend libraries. Do not invent unsupported flags. Keep the builder toolchain separate from the runtime image.

### 7.3 Export binaries

Builder container receives host bind mount `data/runtimes/staging/<id>`. Export executable binaries, dynamic ggml backend `.so` files and any non-system runtime libraries required. Verify with `ldd`/`readelf`, loader tests and `sd-cli --help`/backend device enumeration inside the **actual runtime** base image. Use RPATH or `LD_LIBRARY_PATH` as appropriate; preserve required symlinks. Write a manifest containing Git commit, build flags, toolchain/container base, arch, dependency versions and artifact checksums. Activate only after verification.

### 7.4 Runtime image

Use a slim **compatible** Debian/Ubuntu-style base (not necessarily Alpine, due to glibc compatibility) with Mesa RADV/Vulkan loader, libstdc++, OpenMP and required shared libraries. Bind mount:

```yaml
volumes:
  - ./data/runtimes/current/bin:/opt/sd/bin:ro
  - ./data/models:/models:ro
  - ./data/inputs:/inputs:ro
  - ./data/outputs:/output:rw
  - ./data/logs:/logs:rw
```

The runtime image has **no sole copy** of compiled sd.cpp. It may be rebuilt after `podman system prune` without rerunning the compiler. Artifacts remain versioned on the host. If image is missing, the runner recreates it from lightweight Dockerfile and verified host binaries.

### 7.5 Management commands

```text
mirage runtime status
mirage runtime build [--revision <commit>]
mirage runtime verify
mirage runtime repair
mirage runtime activate <revision>
mirage runtime rollback
```

Keep prior verified release for rollback; don't switch binaries while a job is executing. Clean old versions only on explicit user request. Document ABI compatibility limitations and require rebuild if host/runtime architecture changes incompatibly.

## 8. Download manager

Commands:

```text
mirage models available
mirage models list
mirage models install moss
mirage models install qwen --with-editing
mirage models install zimage
mirage models verify qwen
mirage models remove moss
```

Requirements: manifest-driven dependencies, disk-space check, visible total size, HTTP(S) with optional HF authentication, retries/backoff, resume via Range only when server supports it, `.partial` files, checksum/size verification, atomic rename, skip valid files, resume interrupted downloads, reject corrupt/mismatched partials, clean explicit cancellation, no deletion while model is in an active job. Reuse content-addressed identical files when safe without introducing fragile symlinks across mounts. Provide per-file and overall progress in both CLI and GUI. Offline generation must work once components are installed.

The vision mmproj is optional in basic `qwen` setup, required for `qwen` edit; default to the matching **Q8_0** projector as tested. Verify naming/availability in live repo before release.

## 9. CLI design

Package as `mirage` through Python entry points. Preserve current `generate.py` short flags where practical:

```bash
mirage generate -m zimage -q high -r hd \
  -p 'A red fox reading a book under an oak tree'

mirage generate -m qwen -q fast -r fhd \
  --ref ./car.png -p 'Replace the car with a classic muscle car'

mirage jobs list
mirage jobs show <id>
mirage jobs cancel <id>
mirage jobs retry <id>
mirage gallery list
mirage gallery export <image-id>
mirage models list
mirage config reload
mirage serve
```

Define explicit `--wait` (synchronous) and `--detach`/default queue behavior, and document exit codes. CLI must talk to the running service for queued jobs, or start the same application services in controlled local mode; it must never independently run an uncoordinated GPU job when the service owns the queue. Use argv arrays, never shell concatenation for user inputs.

## 10. Job engine and persistence

Use SQLite (WAL) with schema migrations and clear transaction boundaries. Model generation jobs as immutable resolved requests plus mutable status. States: `queued`, `running`, `completed`, `failed`, `cancelled`, `interrupted`. Include timestamps, model, prompt, size, effective steps/CFG/sampler/scheduler/backend/seed, reference IDs, runtime revision, input/output paths, job logs, duration, error, progress and cancellation info.

- Exactly one GPU inference job active at a time initially; others queue FIFO.
- API/CLI submission returns job ID immediately.
- Browser disconnect/refresh must not stop work.
- Worker supervises one-shot `sd-cli` runtime container/process and captures stdout/stderr.
- Start inference only after checking model files and runtime completeness.
- Cancellation terminates child process **and container** cleanly, without orphaning GPU workloads.
- On restart, reconcile `running` jobs: reattach only where reliably possible; otherwise mark `interrupted`. Never silently claim success or auto-double-run.
- Use unique temporary output paths and validate output file; publish atomically and record metadata only when final PNG exists.
- Maintain raw logs; parse progress opportunistically, not as a correctness dependency. Distinguish waiting, loading, conditioning, sampling, decoding, saving.
- Retry requires explicit request and new job record linked to original; preserve original diagnostic information.

**Filename format** (compatibility requirement):

```text
YYYYMMDD_<model-name>_<size-preset>_<quality-preset>-<serial>.png
```

Example: `20261116_qwen-image-2.1_medium_fast-0001.png`. Serial allocation must never overwrite existing outputs even across concurrent submitters. Store JSON sidecars and reference relationships. Use UUID/database IDs as primary identity independent of filenames. No accidental reuse of deleted serial IDs within the same naming prefix if the database records still exist.

## 11. Media and reference-image handling

Upload/reference images must be validated by decoded content/type, file size and dimensions (not extension alone), copied into controlled persistent storage, and referred to by generated IDs. Never allow path traversal or arbitrary server file access via an HTTP request. Support repeated references for Qwen editing. Record origin, immutable copy, content hash and relationship to generated image; don't delete input while referenced.

Gallery:
- Responsive thumbnail grid, full-resolution lightbox, metadata/details, prompt search, model/date/quality/size filters, sorting, download, re-use settings, select generated image as reference, optional side-by-side comparison.
- Thumbnail generation should use Pillow or another small **server-side** image codec; the no-graphics-dependency constraint means *no desktop GUI stack*, not no image decoding.
- Logical delete/trash first, permanent-delete after confirmation. Ensure referenced inputs and active job files are protected.
- Output PNGs and JSON sidecars remain usable independent of SQLite (document a repair/reindex path).

## 12. HTTP API and live progress

Use FastAPI or another similarly lightweight Python framework. Static UI served by same origin. Suggested endpoints:

```text
GET    /api/health
GET    /api/system
GET    /api/models
GET    /api/models/{id}
POST   /api/models/{id}/install
POST   /api/models/{id}/verify
POST   /api/models/{id}/remove
GET    /api/config/generation
PUT    /api/config/generation
POST   /api/config/generation/reload
POST   /api/config/generation/restore-defaults
POST   /api/uploads
POST   /api/jobs
GET    /api/jobs
GET    /api/jobs/{id}
GET    /api/jobs/{id}/logs
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry
GET    /api/events                 # SSE; polling fallback
GET    /api/images
GET    /api/images/{id}
GET    /api/images/{id}/file
GET    /api/images/{id}/thumbnail
DELETE /api/images/{id}
```

Make endpoints versionable (e.g. `/api/v1`) before API stability commitment. Return structured errors with stable machine-readable codes plus localized UI messages. Long operations are async jobs; request handlers must never block during model download or generation. API clients never submit raw host paths or arbitrary CLI flags.

## 13. Buildless frontend — mandatory technology decision

**No frontend build pipeline, Node, npm, Vite, webpack, React, Vue or Electron.** Use served-as-is `index.html`, plain `.css`, and native browser JavaScript ES modules. Vendored pinned no-build dependencies are permitted only with clear justification, local copies and license tracking. No CDNs. Python serves files as-is; replacing static files and refreshing browser updates UI.

Design expectations: polished compact responsive interface, accessible keyboard navigation and focus styles, icons via local SVG sprite, consistent spacing and typography, subtle shadows, clean dialogs, progress and error states. Avoid developer-console styling or excessive decoration. Use CSS variables/design tokens; no inline-executable JS or unsafe dynamic HTML.

Navigation sections:
1. **Generate** — prompt, model/task, reference drag/drop and preview, resolution and quality selectors from API, advanced parameters, queue submission, preview/result.
2. **Gallery** — image-first grid, filters/search, image detail/metadata, compare, download, use as reference, safe deletion.
3. **Jobs** — active/queued/completed/errors, stage/progress, cancel/retry, expandable logs.
4. **Models** — installed/missing components, download/verify/remove, disk usage and progress.
5. **Settings** — runtime status and build revision, user-adjustable generation presets, backend profiles, GUI language/theme controls, config validation/reload/restore.

Generate form and dropdowns must derive models, capabilities and presets from API, not static JS arrays. Model-specific controls must disappear or disable when unsupported. Browser refresh and navigation never stop jobs.

### 13.1 Themes

Supported theme modes: `system` (default), `light`, `dark`. In `system`, follow `prefers-color-scheme` **live**. Manual override saved in browser `localStorage`. Apply initial theme before first paint to prevent flash of wrong colors, while maintaining strict CSP through a safe static strategy. All screens and controls honor both palettes. Accessible contrast and focus states are mandatory.

### 13.2 English/Russian localization

Support `auto` (default), `en`, `ru`. `auto` uses `navigator.languages` in preference order; choose first supported locale (including regional subtags), otherwise English. Manual override persists in browser storage. Translation dictionaries: `web/locales/en.json` and `web/locales/ru.json`; **no hardcoded visible UI strings** in components. Use `Intl.DateTimeFormat` / `Intl.NumberFormat`. Never translate prompts, filenames, model IDs or user-authored metadata. Validate equal translation keys, matching placeholders and English fallback automatically. `lang` attribute and accessible labels must update on locale change. Locale/theme preferences are per browser, not server-global.

### 13.3 No-build quality checks

Use browser-executable unit/integration smoke tests with no mandatory Node toolchain. Python can run static checks for JSON catalog parity, missing links, assets, JS syntax where available, and a browser automation harness if optionally installed. A full test run must be documented even without JS package manager.

## 14. Settings and security

Default network bind `127.0.0.1`; no public exposure. LAN mode requires explicit opt-in and authentication or trusted reverse proxy. API paths must never expose arbitrary file read/write. Validate HTTP upload sizes/types, safe filename handling and model-repository allowlists. Never use `shell=True` for untrusted inputs. Admin-only endpoint authorization for global settings, runtime builds, model deletion and configuration reset if nonlocal access enabled.

Log paths and secrets carefully: redact HF tokens and credentials. Store optional authentication token in environment/secrets, not JSON manifest or browser. Adopt a simple backup/restore documented procedure for `data/config`, SQLite (consistent backup), models, inputs and outputs.

## 15. Testing and acceptance

Automated tests:
- Config: schema validity, missing keys, invalid widths/steps, atomic reload, last-known-good retention, preset changes reflected in CLI/API, queued job snapshots unaffected.
- Models: verified downloads, resume, incomplete files, corruption, checksum mismatch, disk space, optional Qwen projector dependencies.
- Runtime: exported binaries/libs work inside minimal image; image deletion followed by restart **does not invoke compiler**; broken artifacts trigger repair; activation/rollback safe.
- Job engine: queue ordering, atomic filenames, no overwrites, disconnect survival, cancellation of process/container, crash reconciliation, no duplicate execution, retry semantics.
- Media: images, metadata, references, upload validation, filters, safe trash, gallery reindex.
- GUI: light/dark system and override, en/ru auto and override, responsive forms, browser refresh during job, model installation and gallery interactions.

Provide a **fake sd-cli fixture** for fast integration tests without GPU. Provide optional real-GPU smoke tests for all three models on the Radeon 780M; at least one Qwen edit with Q8_0 mmproj and CPU `te`/`clip_vision` placement. Check log evidence for expected CPU/Vulkan backends and single-segment diffusion where memory permits; do not demand fixed timing across driver revisions.

Acceptance scenarios:
1. Clean host bootstrap, no manual unexplained steps.
2. Install model and dependencies from GUI and CLI.
3. Generate Z-Image Turbo image with a named preset.
4. Edit supplied image in Qwen with projector and CPU/Vulkan profile.
5. Submit multiple jobs and close/reopen browser; queue and history persist.
6. Change `qwen.medium.steps` in JSON, reload, and confirm **new** CLI and GUI jobs use it while old/queued jobs retain their captured values.
7. Switch OS theme while GUI in system mode; change browser locale and verify automatic language selection; manual overrides persist.
8. Delete builder and runtime container images, then regenerate using persistent compiled artifacts **without compilation**.
9. Simulate interruption and verify database/jobs/logs/files remain consistent.
10. Upgrade and roll back runtime while preserving model data, outputs and settings.

## 16. Implementation phases and gates

### Phase 0 — Audit the existing proven POC

Collect original `generate.py`, Compose file, installed component filenames, logs and pinned sd.cpp commit (`master-1106-532d2c1+` observed previously). Reproduce model generation and Qwen edit in a documented run. Verify actual upstream flags against pinned source. **Gate:** known-good baseline documented and reproducible.

### Phase 1 — Python package, registry, external presets, CLI

Create package, configuration service, model registry, validated presets, CLI compatibility and fake-runner tests. No web frontend yet. **Gate:** CLI reproduces existing workflows; edit JSON + reload changes CLI steps; output naming remains collision-safe.

### Phase 2 — Persistent runtime build and recovery

Implement builder image, exported host artifacts and manifest, minimal runtime image, verification, version activation/rollback and recovery after prune. **Gate:** rebuild runtime without rebuilding sd.cpp after image removal.

### Phase 3 — Model downloader and installer

Implement manifest-driven downloads, verification, resume, optional mmproj and install state. **Gate:** empty model store can be populated automatically and verified.

### Phase 4 — SQLite queue, worker, media and logs

Implement durable jobs, supervision, cancellation, crash recovery, metadata, safe files, thumbnail creation. **Gate:** CLI can submit queued jobs; surviving browser/service disconnect semantics and restart reconciliation tested.

### Phase 5 — REST API and SSE

Expose normalized models/config/jobs/gallery APIs with backend validation and progress. **Gate:** API functional without GUI; no inference executed synchronously in request handlers.

### Phase 6 — Buildless bilingual/themed web UI

Deliver served-as-is frontend and all screens. Dynamic preset forms, model installer, gallery and jobs. Automatic OS theme/browser locale; manual switches. **Gate:** desktop/tablet browser use fully functional with no frontend compilation.

### Phase 7 — Hardening, recovery, documentation and release

End-to-end GPU tests, prune recovery, migration, backup, security checks, accessibility, localization audit and clean install. **Gate:** acceptance scenarios in §15 pass.

## 17. Instructions for coding agent

1. Start by inspecting existing source and configuration files supplied by the user; preserve verified paths and flags. Do not replace a working inference backend for stylistic reasons.
2. Before each phase, state the files to create/change and validation commands. Implement the phase fully with tests and documentation, then report exact results and outstanding blockers.
3. Do not build the UI before core job/runtime/config services exist.
4. Do not silently skip or reinterpret hard requirements. Flag conflicts (e.g., build flags differing in pinned sd.cpp revision) with evidence and propose minimal adjustment.
5. Avoid overengineering: no Kubernetes, distributed queues, external database, message broker, JS build chain, Electron or custom model inference implementation.
6. No fabrication of benchmark results. Use actual logs and report measured vs estimated values distinctly.
7. Do not claim build/recovery tested unless you really removed images and verified the host-mounted compiled artifacts continued working.
8. Produce a runnable implementation incrementally, with small commits and a final checklist mapping every acceptance scenario to its test.

**Final deliverables:** repository, bootstrap script, persistent builder/runtime Compose setup, tested CLI, verified model manifests/downloads, preset JSON and reload, REST API, buildless English/Russian themed GUI, gallery, queue, tests, install/upgrade/backup documentation.
