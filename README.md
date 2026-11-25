# Mirage

<img src="assets/mirage_logo.png" alt="Mirage logo" width="240">

Mirage is a local, persistent image-generation application for MOSS-Image-SFT, Qwen-Image 2.1, and Z-Image Turbo, built around `stable-diffusion.cpp`.

It provides a Python CLI, JSON API with server-sent events, a buildless browser UI, one SQLite-backed queue, resumable model installation, and a searchable gallery. The browser and CLI share the same backend and cannot start uncoordinated GPU jobs.

## Requirements

- Linux on x86-64 (the initial target is ALT Linux with AMD RADV/Vulkan).
- Podman or Docker, including permission to use its API socket.
- Python 3.11 or newer, Git, and standard Unix utilities. The installer uses only the Python standard library. Podman Compose is optional; the default launcher does not require it.
- A visible `/dev/dri` render device and working Vulkan driver for GPU generation.
- 8 GB free space before models, plus 6.7–10.7 GB per selected model.
- Internet access for container packages, source submodules, and only the models you select.

`uv`, Python packages, C++ build tools, and frontend toolchains are not required on the host for normal installation. Application dependencies are resolved inside containers with `uv` using the committed `uv.lock`. The UI needs no Node.js, npm, or frontend build.

## Install

Clone with all source submodules:

```sh
git clone --recursive <your-mirage-remote>
cd mirage
./install.sh
```

The installer opens an interactive model checklist. With no `dialog`/`whiptail`, it offers a numbered fallback. Select `qwen-edit` to install both Qwen-Image 2.1 and its Q8_0 vision projector. The container build itself never downloads model weights; downloads start only after your selection.

Open **http://127.0.0.1:8765**. The `mirage` command is linked into `~/.local/bin`; add that directory to `PATH` if needed.

To add models later, rerun `./install.sh`. Installed items are checked, previously skipped items can be added, and nothing is implicitly removed. You can also run:

```sh
mirage models install moss --wait
mirage models install qwen --with-editing --wait
mirage models install zimage --wait
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

Other options include `--data-dir PATH`, `--prefix PATH`, `--runtime podman|docker`, `--skip-build`, `--no-start`, and `--allow-no-gpu`. Run `./install.sh --help` for details. Local installs build the current working tree, including uncommitted changes, without modifying Git state.

### Standalone installation

A standalone bootstrap reads the latest release metadata, clones that tag with recursive pinned submodules, and stores it in a persistent versioned source directory. User data and installer state are kept outside that source directory. Until the canonical release host is configured, provide the release endpoint and repository explicitly (a local release fixture uses the same interface):

```sh
curl -fsSL <release-host>/install.py | \
  MIRAGE_RELEASE_API=<latest-release-json-url> \
  MIRAGE_RELEASE_REPOSITORY=<release-git-url> \
  python3 - --non-interactive --models none --skip-models
```

Interactive prompts use `/dev/tty`, so this also works when the script arrives through a pipe. Non-interactive installs require explicit options where a choice or credential is needed. Installer options take precedence in this order: command-line arguments, `MIRAGE_*` environment variables, persisted installation state, then defaults.

### Update and remove

```sh
./install.sh update --non-interactive --models none --skip-models
./install.sh remove --yes --non-interactive
```

Non-secret state is stored in `~/.local/state/mirage/installation.json` by default. Updates use a per-installation lock, close database admission, refuse queued or running jobs, drain admitted writes, verify isolated candidates, back up SQLite and configuration, and record each live-state action in a recovery journal. An interrupted operation is inspected and recovered before another operation can start. Removal requires confirmation or `--yes`, refuses active jobs, removes the recorded application stack and owned launcher, and preserves models, images, configuration, runtime releases, and other user data.

## Generate

```sh
# Queue and return immediately
mirage generate -m zimage -q high -r hd \
  -p 'A red fox reading a book under an oak tree'

# Wait for completion
mirage generate --wait -m qwen -q fast -r fhd \
  --ref ./car.png -p 'Replace the car with a classic muscle car'

mirage jobs list
mirage jobs show <job-id>
mirage jobs logs <job-id>
mirage jobs cancel <job-id>
mirage jobs retry <job-id>
mirage gallery list --search fox
mirage gallery export <image-id> ./exported.png
```

Closing a terminal or browser does not cancel a job. After a service restart, jobs that were running become `interrupted`; they are never silently rerun. Retry creates a new job and a new output filename.

Presets live in `data/config/generation-presets.json`. The exact sizes are `sd` (512×512), `hd` (1024×1024, the default), and `fhd` (1920×1080). Width and height overrides are not accepted; model-specific fast, medium, and high quality settings remain editable. Invalid reloads report an error and leave the previous validated snapshot active. Each submission captures its resolved sampling values, config revision, complete backend profile, sd.cpp commit, content-derived runtime build ID, and immutable runtime image ID. Later edits and runtime activation do not alter queued jobs or retries. FHD results are center-cropped to exactly 1920×1080 before hashing, thumbnails, sidecars, and gallery publication.

The gallery supports fullscreen and metadata dialogs, keyboard dismissal, focus restoration, and dimension-based settings reuse. Its width and height filters include verified historical PNG dimensions and combine with the other filters using AND. The CLI retains `gallery list --resolution`, `--width`, and `--height` filters.

```sh
mirage config show
mirage config validate
mirage config reload
mirage config restore-defaults
```

## Models

`config/models.json` is the compatibility registry. It records exact Hugging Face repositories and paths, expected bytes and SHA-256 hashes, licenses, required/optional components, and the minimum tested `sd.cpp` revision. Qwen editing requires the matching projector; MOSS and Z-Image reject reference images.

```sh
mirage models available
mirage models list
mirage models verify qwen --with-editing
mirage models remove moss
```

Downloads use `.partial` files, bounded retries, HTTP Range only with a valid range response, disk-space checks, atomic publication, and final checksum verification. `HF_TOKEN` is read from the environment when needed and is never written to model metadata. Removal is blocked while the model has queued or running jobs.

## Runtime lifecycle and recovery

The builder compiles Vulkan-enabled `sd-cli`, `sd-server`, shared GGML backends, and CPU variants in an Ubuntu builder container. It exports binaries and libraries to:

```text
data/runtimes/releases/<build-id>/bin
data/runtimes/releases/<build-id>/manifest.json
data/runtimes/current -> releases/<build-id>
```

The runtime image contains only the compatible Mesa/glibc environment. Inference containers mount host artifacts read-only, models and inputs read-only, and outputs/logs read-write. Therefore deleting builder or runtime images does not delete compiled artifacts or force compilation.

```sh
mirage runtime status
mirage runtime build
mirage runtime verify --devices
mirage runtime repair
mirage runtime activate <build-id>
mirage runtime rollback
```

`mirage runtime repair` uses an existing verified release when available and recreates only the lightweight runtime image. Activation and rollback are refused during active generation. Releases and runtime images referenced by jobs or retained releases are never pruned automatically. Build IDs cover the actual sd.cpp working tree (including dirty and nested submodule changes), build inputs, and base image identities. Manifests record the sd.cpp commit, build ID, immutable runtime image identity, architecture, CMake flags, and artifact hashes. Execution, verification, activation, status, retry, and rollback all expose and use the same identity.

Rootless Podman uses `keep-id` for the app and receives the host's actual supplementary group IDs through its raw API (the API does not accept the CLI-only `keep-groups` token). Docker uses its standard socket and socket group. No Linux group literally named `render` is assumed.

## Persistent data

The default data root is `./data`; override it with `--data-dir` or `MIRAGE_DATA_DIR`.

- `config/`: editable generation and backend settings, preserved on upgrades.
- `models/`: verified model components.
- `downloads/`: resumable partial files.
- `inputs/`: decoded, immutable reference images.
- `outputs/`: PNGs and standalone JSON sidecars.
- `thumbnails/`: server-generated gallery thumbnails.
- `logs/`: durable job and model-install logs.
- `runtimes/`: versioned host binaries and activation links.
- `mirage.sqlite3`: jobs, events, references, and gallery index.

Outputs use `YYYYMMDD_<model>_<resolution>_<quality>-<serial>.png`. Publication is atomic and existing outputs are never overwritten. A recoverable publication state coordinates the final PNG, thumbnail, sidecar, and database insert; incomplete states are reconciled on service startup. Gallery deletion is trash-first; permanent deletion is a separate explicit operation. `mirage gallery reindex` restores database entries from output PNGs and sidecars.

## Service and security

```sh
mirage start
mirage stop
mirage restart
mirage logs
# Or run in the foreground:
mirage serve
```

The API binds to `127.0.0.1` by default. LAN binding requires `MIRAGE_API_TOKEN`; without it startup fails. Deploy only on a trusted home LAN or behind an authenticated HTTPS reverse proxy, not on the public Internet.

CLI and integration clients use `Authorization: Bearer <MIRAGE_API_TOKEN>`. The browser instead signs in through a small login form and receives an HttpOnly, SameSite=Strict session cookie, so API requests, EventSource, and image URLs all work without exposing the token to JavaScript. Cookie-authenticated mutations require a per-session CSRF header. Sessions are held in memory; restart the service to invalidate all sessions and require signing in again.

Two browser roles are available:

- `MIRAGE_API_TOKEN` grants the admin role, including global settings, model removal, and runtime operations.
- Optional `MIRAGE_BROWSER_TOKEN` grants a non-admin role for generation, jobs, gallery, downloads, and model installation, but not administrative operations. The server enforces this distinction independently of hidden UI controls.

Uploads are read in bounded chunks, then decoded and normalized by Pillow. API clients submit generated reference IDs rather than host paths or arbitrary command-line flags. File endpoints accept only database IDs. User input is passed as argv arrays, never shell-concatenated.

The service needs the writable container API socket to create supervised one-shot inference containers. This is a privileged trust boundary: an application compromise can control containers, and rootful Docker socket access can effectively provide host-level privileges. Do not expose the service to untrusted users. A future deployment variant should separate the engine-controlling worker from the HTTP-facing process.

`compose.yaml` is available for users who prefer Compose. Set `MIRAGE_APP_IMAGE` to the immutable image ID or unique tag from the app release manifest, plus `MIRAGE_HOST_DATA_DIR`, `CONTAINER_SOCKET`, and `MIRAGE_CONTAINER_ENGINE`. The shell launcher remains the reference path.

## Backup, restore, and upgrade

```sh
mirage backup
```

This uses SQLite's online backup API and copies config, inputs, and outputs into `data/backups/<UTC timestamp>/`. Models and runtimes are excluded because they can be restored from pinned manifests. For a complete offline backup, also copy those directories.

To restore, stop the service, preserve the current data directory, then restore the backup's database and state directories into a new data root. Start with `MIRAGE_DATA_DIR=/path/to/restored mirage start` or set that directory during installation.

Upgrade with `./install.sh update`. User configuration, models, outputs, logs, and database files are preserved and backed up before offline migrations. If verification or cutover fails before the durable commit point, recovery restores the original database, configuration, runtime link, and running or stopped state. A failure after that point never restores the old database.

## API

Versioned endpoints are under `/api/v1` (the main unversioned aliases are also available). They cover browser login/session/logout, health/system, models, generation config, uploads, jobs/logs/cancel/retry, events, images/files/thumbnails, gallery reindex, and runtime status/verify/activation/rollback. Long downloads and generation return a job immediately. `GET /api/v1/events` provides SSE; the UI falls back to polling.

## Tests and source packaging

Host tests use `uv`; container tests need only the container engine:

```sh
uv run --locked --dev pytest -q
./scripts/test.sh
```

Tests use tiny payloads, sparse component fixtures, and `MIRAGE_FAKE_SD=1`; they do not download models or require a GPU. Real Vulkan generation and full-size model checksum verification are separate acceptance steps.

Create a self-contained source archive, including the pinned submodule working tree but excluding `.git`, virtual environments, data, and build outputs:

```sh
./scripts/package.sh
```

The archive is written to `dist/`. When working from Git instead, always clone recursively.

## License

This project's first-party software is available under the [MIT License](LICENSE).
The pinned [stable-diffusion.cpp submodule](https://github.com/leejet/stable-diffusion.cpp/blob/79dc7ca9074d79419a46380cdb95a4524db9b74c/LICENSE) carries its own MIT license.
Model weights are downloaded from Hugging Face on the deployment host and are governed by the model licenses listed in [config/models.json](config/models.json).
