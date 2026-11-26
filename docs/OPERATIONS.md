# Mirage setup and maintenance

The README walks through installing the web interface and creating your first image. [Quick start](../README.md#1-install-the-web-interface)

## Install with custom options

From the Mirage checkout, run `./install.sh --help` for all options. If you cloned without source dependencies, run `git submodule update --init --recursive` first.

```sh
./install.sh --data-dir "$HOME/mirage-data" --port 9000 --runtime docker
```

By default Mirage uses `./data`, port `8765`, and Podman when both engines are installed. The installer remembers the data directory, engine, access mode, and port for future commands. Use `--prefix` to change where the launcher is linked.

### Unattended setup and local model files

Pass a model ID or a comma-separated list. `all` includes Qwen’s editing component. Use `--skip-models` to set up the application and add models later.

```sh
./install.sh --non-interactive --models zimage
./install.sh --non-interactive --models moss,qwen-edit
./install.sh --non-interactive --skip-models
```

If you already have the model files, pass their parent directory to `--import-models`. Inside it, use the filenames and relative paths from the model registry. For example, the Z-Image Turbo files go under `zimage/`. The installer checks file sizes and imports the files from disk. Container setup may still need internet access. [Model registry](../config/models.json)

```sh
./install.sh --non-interactive --models zimage --import-models /path/to/models
mirage models verify zimage
```

If a download is interrupted, run the same `mirage models install` command again to resume. Set `HF_TOKEN` before installation or service startup if Hugging Face requires a token for the files you need.

### Install from a tagged release

The standalone installer needs a release metadata URL and a Git repository. It reads the release tag and puts the source for that tag in a versioned directory, with persistent data stored separately.

```sh
python3 install.py --standalone --release-api "$release_api" \
  --repository "$release_repository" --non-interactive --skip-models
```

## Use the command line

The installer links `mirage` into `~/.local/bin`. If your shell cannot find it, add that directory to your `PATH` or run `~/.local/bin/mirage` directly. Browser and terminal submissions use the same queue.

```sh
mirage models install qwen --with-editing --wait
mirage generate --wait -m zimage -p 'A red fox reading under an oak tree'
mirage jobs list
mirage gallery list --search fox
```

Use `mirage start` and `mirage stop` to control the web service, or `mirage logs` to follow its logs.

## Manage jobs and settings

| Task | Command |
| --- | --- |
| Follow a job | `mirage jobs wait JOB_ID` |
| Inspect a job’s logs | `mirage jobs logs JOB_ID` |
| Cancel or retry | `mirage jobs cancel JOB_ID` or `mirage jobs retry JOB_ID` |
| Export an image | `mirage gallery export IMAGE_ID ./exported.png` |
| Check installed models | `mirage models list` |

Get job and image IDs from `mirage jobs list` and `mirage gallery list`. After a service restart, a job that was running is marked `interrupted`. Retrying creates a new job and output filename.

The resolution presets are `sd` (512×512), `hd` (1024×1024, the default), and `fhd` (1920×1080). Change model-specific quality settings in **Settings** or edit `data/config/generation-presets.json` and run `mirage config validate` followed by `mirage config reload`. Jobs already in the queue keep the settings they were submitted with.

## Allow access from another device

In the default `local` mode, Mirage listens on `127.0.0.1` and gives local users administrator access without a sign-in. For access from other devices, use `authenticated` mode on a trusted LAN or behind an authenticated HTTPS reverse proxy. The service can control containers through the engine socket. Keep it off the public internet.

For an existing installation, set both tokens and update the access mode. For a new installation, omit `update`.

```sh
export MIRAGE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MIRAGE_BROWSER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
./install.sh update --mode authenticated --non-interactive --skip-models
```

Open the Mirage server’s LAN address and port, or the proxy’s HTTPS URL, and sign in with one of the tokens.

- `MIRAGE_API_TOKEN` grants administrator access in the browser and authenticates CLI and API requests. API clients send it in the `Authorization: Bearer` header.
- `MIRAGE_BROWSER_TOKEN` allows browser users to generate images, manage jobs, use the gallery, and install models. Administrative settings and runtime controls require the administrator token.

Save the tokens securely. They must differ and each must contain 16–64 ASCII letters, digits, underscores, or hyphens. Export the API token for CLI requests and both tokens before recreating the service. To rotate tokens, export the new values and rerun the installer update. To change the port, pass `--port` to the installer.

For Compose, set `MIRAGE_APP_IMAGE` to the built application image ID or unique tag, `MIRAGE_HOST_DATA_DIR` to the absolute data path, `CONTAINER_SOCKET` to the engine socket, and `MIRAGE_CONTAINER_ENGINE` to the engine name. The authenticated file also requires both tokens. Set `MIRAGE_PORT` to override `8765`. [Local Compose](../compose.yaml) · [Authenticated Compose](../compose.authenticated.yaml)

## Troubleshoot

| If you see this | What to check |
| --- | --- |
| The browser cannot connect | Use the port printed by the installer. Run `mirage start` and check `mirage logs`. |
| The installer cannot find the engine socket | For rootless Podman with systemd, run `systemctl --user enable --now podman.socket`. For Docker, check that the daemon is running and your user can access its socket. |
| A model is missing from your installation | Check **Models** or run `mirage models list`. Install the model and wait for the download job to finish before submitting a prompt. For Qwen editing, include its optional component. |
| Mirage cannot use the GPU | Check that your user can access a render device under `/dev/dri`. With Mirage running, use `mirage runtime verify --devices` to check device visibility in the runtime. |
| The runtime image is missing | Run `mirage runtime repair` to recreate it from a verified runtime release when available. If you use a custom data directory, set `MIRAGE_DATA_DIR` to that path for this command. |

Use `mirage runtime status` to see the active release and `mirage runtime rollback` to switch back to the previous release when no generation jobs are active.

## Update or remove Mirage

Wait for queued and running jobs to finish. To update a Git checkout, first check out the version you want and run `git submodule update --init --recursive`. Then run the installer from that checkout.

```sh
./install.sh update --non-interactive --skip-models
```

The update verifies the new build, backs up the database and configuration, and keeps the models, images, and other user data. If an update is interrupted, rerun the installer so it can recover before starting another operation.

To uninstall, run `./install.sh remove` and confirm the prompt. This removes the application containers and installed launcher while retaining your data directory. For unattended removal, use `./install.sh remove --yes --non-interactive`.

## Back up and restore data

The data directory contains the SQLite database, settings, reference images, generated PNGs and metadata, model files, and compiled runtimes. It defaults to `./data` for a checkout. Replace `./data` below if you selected a different path.

Wait for active jobs to finish, then stop Mirage and copy the full directory. Allow enough free space for the models and runtimes as well as the images.

```sh
mirage stop && cp -a ./data "$HOME/mirage-backup-$(date +%Y%m%d-%H%M%S)"
mirage start
```

To restore, stop Mirage if it is running and copy the backup to a new data directory. Point the installation at that directory so the launcher remembers it.

```sh
./install.sh update --data-dir "$HOME/mirage-restored" --non-interactive --skip-models
```

On a fresh machine, omit `update` and use the same data directory option. To rebuild the gallery index from existing PNGs and JSON metadata, run `mirage gallery reindex`.

## API and development

Mirage’s JSON API is under `/api/v1`. Fetch `/openapi.json` on your Mirage server for the API schema. The server-sent event stream is at `/api/v1/events`.

Run `uv run --locked --dev pytest -q` on the host or `./scripts/test.sh` with a container engine. The automated suite uses fixtures and runs without a GPU. [Acceptance checklist](ACCEPTANCE.md)

Run `./scripts/package.sh` to create a source archive in `dist/` with the local submodule tree included.
