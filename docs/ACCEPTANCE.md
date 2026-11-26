# Acceptance checklist

This checklist distinguishes automated, non-GPU checks from checks that require the full local model set and target GPU hardware. The original proof-of-concept runs used Radeon/Vulkan; NVIDIA/CUDA requires separate target-host validation. No timing or perceptual-quality claim is inferred from the original proof-of-concept runs.

| Scenario | Verification |
|---|---|
| Clean bootstrap and preflight | `tests/test_install.py`; interactive/non-interactive install on target host |
| Add models later without removal | Installer selection logic; `mirage models install`; target-host rerun |
| Model URLs, paths, sizes, optional projector | Live Hugging Face API review; `tests/test_models.py`; full checksum via `mirage models verify` |
| Config defaults and validation | `tests/test_config.py` |
| Invalid reload retains last known good | `tests/test_config.py` |
| New jobs use changes; queued jobs retain snapshots | `tests/test_jobs.py::test_queued_job_keeps_config_snapshot` |
| Unique output names and no overwrites | `tests/test_config.py`, generation/retry tests |
| Queue completion, metadata, thumbnails, sidecars | `tests/test_jobs.py` with fake `sd-cli` |
| Disconnect-safe queue | Service owns workers; API returns immediately; browser/CLI restart check on target |
| Cancellation without publication | `tests/test_jobs.py::test_cancellation_does_not_publish_image` |
| Restart reconciliation and explicit retry | `tests/test_jobs.py`; real service restart check on target |
| Qwen edit projector, 4-bit prefix cache, and CPU/Vulkan split | Argument tests in `tests/test_jobs.py`; real Qwen edit and log inspection required |
| Reference rejection for unsupported models | `tests/test_jobs.py` |
| Resumable downloads and checksum rejection | `tests/test_downloader.py` with tiny local payload |
| Upload content/path safety | `tests/test_models.py` |
| Runtime artifact integrity and rollback | `tests/test_runtime.py`; real builder/runtime test required |
| Image deletion recovery without compilation | Remove builder/runtime images on target, run `mirage runtime repair` and generation; confirm exported host manifest remains |
| Browser cookie login, CSRF, bearer compatibility, roles | `tests/test_auth.py`; packaged LAN E2E for login, SSE, generation, settings, and image download |
| Local/authenticated exposure and token validation | `tests/test_auth.py`, `tests/test_installer.py`; inspect published ports and verify unauthenticated LAN requests fail |
| Custom port persistence and no host networking | Installer and launcher target-host runs; inspect containers and exercise CLI/API through the selected port |
| Backend/runtime snapshot immutability | `tests/test_jobs.py`; real inference mounts the submitted release |
| Publication recovery after interruption | `tests/test_jobs.py::test_publication_recovers_after_database_failure` |
| Streaming upload memory limit | `tests/test_models.py` with omitted and misleading Content-Length |
| English/Russian catalogs and local static assets | `tests/test_frontend.py`; browser locale/manual override check |
| System/light/dark themes | Browser check, including live OS theme change and persistent manual override |
| Responsive UI and browser refresh during jobs | Browser integration check on desktop/tablet widths |
| Gallery filters/download/reference/trash | Service tests plus browser integration check |
| Backup/restore | SQLite backup command plus restore into a fresh data root on target |
| Fixed SD/HD/FHD presets and rejected overrides | `tests/test_sizes.py`, `tests/test_config.py` |
| Preset backup/migration and preserved sampling | `tests/test_sizes.py::test_legacy_migration_preserves_sampling_and_warns_on_fallback` |
| FHD crop, PNG/hash/sidecar/gallery agreement | `tests/test_sizes.py`; pinned-runtime Qwen, MOSS, and Z-Image FHD runs |
| Historical PNG dimensions and AND filters | `tests/test_sizes.py::test_historical_dimensions_backfill_and_filters` |
| Fullscreen natural sizing, focus, polling, locales | Browser integration checks on desktop and mobile in English and Russian |
| Mirage logo and favicon in light/dark themes | README rendering and deployed browser header/tab checks |
| Runtime source/build/image provenance | `tests/test_provenance.py`, `tests/test_runtime.py` |
| Maintenance, busy refusal, and write drain | `tests/test_maintenance.py`; live cutover with an in-flight request |
| Installer source selection and recovery journal | `tests/test_installer.py`; concurrent installers and injected verifier/interruption failures |
| Install, update, remove without model downloads | Docker and Podman target-host runs; verify persistent data survives removal |
| Real MOSS, Qwen, Z-Image generation | Optional hardware smoke runs using already installed models; inspect logs and outputs |
| NVIDIA/CUDA installation and generation | On an NVIDIA host, verify automatic CUDA selection, `mirage runtime verify --devices`, all three models, Qwen editing, cancellation, and service restart; inspect logs for `cuda0` and valid outputs |

## Commands

```sh
uv run --locked --dev pytest -q
./scripts/test.sh
./install.sh --non-interactive --models none --skip-models
```

The final command exercises preflight/build without selecting model downloads. Do not substitute sparse fixtures for real-model acceptance.
