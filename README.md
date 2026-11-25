# SD Workbench

A local, persistent image-generation workbench for MOSS-Image-SFT, Qwen-Image 2.1, and Z-Image Turbo, built around the tested `stable-diffusion.cpp` commit `532d2c15c430a785a712b2d94281f04e733008a4`.

It provides a Python CLI, JSON API with server-sent events, a buildless browser UI, one SQLite-backed queue, resumable model installation, and a searchable gallery. The browser and CLI share the same backend and cannot start uncoordinated GPU jobs.

## Requirements

- Linux on x86-64 (the initial target is ALT Linux with AMD RADV/Vulkan).
- Podman or Docker, including permission to use its API socket.
- Git and standard Unix utilities. Podman Compose is optional; the default launcher does not require it.
- A visible `/dev/dri` render device and working Vulkan driver for GPU generation.
- 8 GB free space before models, plus 6.7–10.7 GB per selected model.
- Internet access for container packages, source submodules, and only the models you select.

Python, `uv`, Python packages, C++ build tools, and frontend toolchains are not required on the host. Python dependencies are resolved and installed with `uv` using the committed `uv.lock`. The UI needs no Node.js, npm, or frontend build.

## Install

Clone with all source submodules:

```sh
git clone --recursive <your-sd-workbench-remote>
cd sd_workbench
./install.sh
```

The installer opens an interactive model checklist. With no `dialog`/`whiptail`, it offers a numbered fallback. Select `qwen-edit` to install both Qwen-Image 2.1 and its Q8_0 vision projector. The container build itself never downloads model weights; downloads start only after your selection.

Open **http://127.0.0.1:8765**. The `sdwb` command is linked into `~/.local/bin`; add that directory to `PATH` if needed.

To add models later, rerun `./install.sh`. Installed items are checked, previously skipped items can be added, and nothing is implicitly removed. You can also run:

```sh
sdwb models install moss --wait
sdwb models install qwen --with-editing --wait
sdwb models install zimage --wait
```

### Non-interactive and offline-model installation

```sh
./install.sh --non-interactive --models zimage
./install.sh --non-interactive --models qwen-edit
./install.sh --non-interactive --models all
./install.sh --non-interactive --models none --skip-models
```

If the exact component files are already downloaded, import them without network access to Hugging Face. The importer validates sizes, uses hard links where possible, and otherwise copies through a temporary file:

```sh
./install.sh --non-interactive --models all \
  --import-models /path/to/existing/models
```

The source layout must be `models/moss`, `models/qwen`, and `models/zimage`, matching `config/models.json`. Subsequent `models install` verifies files and downloads only missing components.

Other options include `--data-dir PATH`, `--prefix PATH`, `--runtime podman|docker`, `--skip-build`, `--no-start`, and `--allow-no-gpu`. Run `./install.sh --help` for details.

## Generate

```sh
# Queue and return immediately
sdwb generate -m zimage -q high -r large \
  -p 'A red fox reading a book under an oak tree'

# Wait for completion
sdwb generate --wait -m qwen -q fast -r medium \
  --ref ./car.png -p 'Replace the car with a classic muscle car'

sdwb jobs list
sdwb jobs show <job-id>
sdwb jobs logs <job-id>
sdwb jobs cancel <job-id>
sdwb jobs retry <job-id>
sdwb gallery list --search fox
sdwb gallery export <image-id> ./exported.png
```

Closing a terminal or browser does not cancel a job. After a service restart, jobs that were running become `interrupted`; they are never silently rerun. Retry creates a new job and a new output filename.

Presets live in `data/config/generation-presets.json`. Edit the file and run `sdwb config reload`, or use the Settings screen. Invalid reloads report an error and leave the previous validated snapshot active. Each submission captures its resolved sampling values, config revision, complete backend profile, and immutable runtime revision. Later edits and runtime activation do not alter queued jobs; execution mounts the exact release under `runtimes/releases/<revision>`. Releases are not deleted automatically.

```sh
sdwb config show
sdwb config validate
sdwb config reload
sdwb config restore-defaults
```

## Models

`config/models.json` is the compatibility registry. It records exact Hugging Face repositories and paths, expected bytes and SHA-256 hashes, licenses, required/optional components, and the minimum tested `sd.cpp` revision. Qwen editing requires the matching projector; MOSS and Z-Image reject reference images.

```sh
sdwb models available
sdwb models list
sdwb models verify qwen --with-editing
sdwb models remove moss
```

Downloads use `.partial` files, bounded retries, HTTP Range only with a valid range response, disk-space checks, atomic publication, and final checksum verification. `HF_TOKEN` is read from the environment when needed and is never written to model metadata. Removal is blocked while the model has queued or running jobs.

## Runtime lifecycle and recovery

The builder compiles Vulkan-enabled `sd-cli`, `sd-server`, shared GGML backends, and CPU variants in an Ubuntu builder container. It exports binaries and libraries to:

```text
data/runtimes/releases/<revision>/bin
data/runtimes/releases/<revision>/manifest.json
data/runtimes/current -> releases/<revision>
```

The runtime image contains only the compatible Mesa/glibc environment. Inference containers mount host artifacts read-only, models and inputs read-only, and outputs/logs read-write. Therefore deleting builder or runtime images does not delete compiled artifacts or force compilation.

```sh
sdwb runtime status
sdwb runtime build
sdwb runtime verify --devices
sdwb runtime repair
sdwb runtime activate <revision>
sdwb runtime rollback
```

`sdwb runtime repair` uses an existing verified release when available and recreates only the lightweight runtime image. Activation and rollback are refused during active generation. Releases are never pruned automatically. The build manifest includes revision, architecture, base images, CMake flags, and artifact hashes; rebuild after an incompatible architecture or ABI change.

Rootless Podman uses `keep-id` for the app and receives the host's actual supplementary group IDs through its raw API (the API does not accept the CLI-only `keep-groups` token). Docker uses its standard socket and socket group. No Linux group literally named `render` is assumed.

## Persistent data

The default data root is `./data`; override it with `--data-dir` or `SDWB_DATA_DIR`.

- `config/`: editable generation and backend settings, preserved on upgrades.
- `models/`: verified model components.
- `downloads/`: resumable partial files.
- `inputs/`: decoded, immutable reference images.
- `outputs/`: PNGs and standalone JSON sidecars.
- `thumbnails/`: server-generated gallery thumbnails.
- `logs/`: durable job and model-install logs.
- `runtimes/`: versioned host binaries and activation links.
- `workbench.sqlite3`: jobs, events, references, and gallery index.

Outputs use `YYYYMMDD_<model>_<resolution>_<quality>-<serial>.png`. Publication is atomic and existing outputs are never overwritten. A recoverable publication state coordinates the final PNG, thumbnail, sidecar, and database insert; incomplete states are reconciled on service startup. Gallery deletion is trash-first; permanent deletion is a separate explicit operation. `sdwb gallery reindex` restores database entries from output PNGs and sidecars.

## Service and security

```sh
sdwb start
sdwb stop
sdwb restart
sdwb logs
# Or run in the foreground:
sdwb serve
```

The API binds to `127.0.0.1` by default. LAN binding requires `SDWB_API_TOKEN`; without it startup fails. Deploy only on a trusted home LAN or behind an authenticated HTTPS reverse proxy, not on the public Internet.

CLI and integration clients use `Authorization: Bearer <SDWB_API_TOKEN>`. The browser instead signs in through a small login form and receives an HttpOnly, SameSite=Strict session cookie, so API requests, EventSource, and image URLs all work without exposing the token to JavaScript. Cookie-authenticated mutations require a per-session CSRF header. Sessions are held in memory; restart the service to invalidate all sessions and require signing in again.

Two browser roles are available:

- `SDWB_API_TOKEN` grants the admin role, including global settings, model removal, and runtime operations.
- Optional `SDWB_BROWSER_TOKEN` grants a non-admin role for generation, jobs, gallery, downloads, and model installation, but not administrative operations. The server enforces this distinction independently of hidden UI controls.

Uploads are read in bounded chunks, then decoded and normalized by Pillow. API clients submit generated reference IDs rather than host paths or arbitrary command-line flags. File endpoints accept only database IDs. User input is passed as argv arrays, never shell-concatenated.

The service needs the writable container API socket to create supervised one-shot inference containers. This is a privileged trust boundary: an application compromise can control containers, and rootful Docker socket access can effectively provide host-level privileges. Do not expose the service to untrusted users. A future deployment variant should separate the engine-controlling worker from the HTTP-facing process.

`compose.yaml` is available for users who prefer Compose. Set `SDWB_HOST_DATA_DIR`, `CONTAINER_SOCKET`, and `SDWB_CONTAINER_ENGINE` first. The shell launcher remains the reference path.

## Backup, restore, and upgrade

```sh
sdwb backup
```

This uses SQLite's online backup API and copies config, inputs, and outputs into `data/backups/<UTC timestamp>/`. Models and runtimes are excluded because they can be restored from pinned manifests. For a complete offline backup, also copy those directories.

To restore, stop the service, preserve the current data directory, then restore the backup's database and state directories into a new data root. Start with `SDWB_DATA_DIR=/path/to/restored sdwb start` or set that directory during installation.

Upgrade with `git pull`, `git submodule update --init --recursive`, and `./install.sh`. User configuration, models, outputs, logs, and database files are not replaced by defaults. Review `config/*.default.json` when a schema migration is announced.

## API

Versioned endpoints are under `/api/v1` (the main unversioned aliases are also available). They cover browser login/session/logout, health/system, models, generation config, uploads, jobs/logs/cancel/retry, events, images/files/thumbnails, gallery reindex, and runtime status/verify/activation/rollback. Long downloads and generation return a job immediately. `GET /api/v1/events` provides SSE; the UI falls back to polling.

## Tests and source packaging

Host tests use `uv`; container tests need only the container engine:

```sh
uv run --locked --dev pytest -q
./scripts/test.sh
```

Tests use tiny payloads, sparse component fixtures, and `SDWB_FAKE_SD=1`; they do not download models or require a GPU. Real Vulkan generation and full-size model checksum verification are separate acceptance steps.

Create a self-contained source archive, including the pinned submodule working tree but excluding `.git`, virtual environments, data, and build outputs:

```sh
./scripts/package.sh
```

The archive is written to `dist/`. When working from Git instead, always clone recursively.
