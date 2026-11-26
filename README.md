# Mirage

<img src="assets/mirage_logo.png" alt="Mirage logo" width="240">

Mirage is a web interface for generating and editing images on your Linux machine. In your browser, write a prompt, track your jobs, and browse or download the results. Mirage runs [`stable-diffusion.cpp`](https://github.com/leejet/stable-diffusion.cpp) locally.

## 1. Install the web interface

You’ll need x86-64 Linux, a Vulkan GPU with a render device under `/dev/dri`, Python 3.11+, and Podman or Docker with access to its API socket. Allow at least 8 GB of free space for setup, plus 6.7 GB for the starter model, and an internet connection.

The installer builds Mirage in containers and starts the web service. It uses the latest published release, the newest version-sorted Git tag if no release is published, or Git HEAD if there are no tags.

```sh
curl -fsSL https://raw.githubusercontent.com/dshein-alt/mirage/master/install.sh | sh
```

Run the same installation with Python’s standard library if you have no `curl`. [Python-only installer](docs/OPERATIONS.md#install-with-python)

In the setup prompts, choose **local** access from this machine or **authenticated** access with sign-in tokens, then choose a port and the models to download. The defaults are **local**, port **8765**, and **Z-Image Turbo**.

When the installer prints `Mirage is ready`, open the address it shows. The default is **[http://127.0.0.1:8765](http://127.0.0.1:8765)**. For authenticated access, sign in with your browser token.

## 2. Add a model in the browser

If you skipped Z-Image Turbo in setup, open **Models**, find **Z-Image Turbo**, and click **Install**. Wait for the download job to finish in **Jobs**. You can return to **Models** later to add MOSS-Image-SFT or Qwen-Image 2.1.

## 3. Generate and find your first image

1. Open **Generate** and select **Z-Image Turbo**. Enter a prompt such as `A red fox reading under an oak tree`, choose a resolution and quality, then click **Queue generation**.
2. Follow progress in **Jobs**. You can close the browser tab while Mirage works.
3. Open **Gallery** to browse or search your finished images. Click an image’s prompt to open its details, then click **Download** to save the PNG. Mirage also stores PNGs and their metadata in `data/outputs/` by default.

## Edit an existing image

In **Models**, click **Install** on Qwen-Image 2.1. If Qwen is already installed without its editing component, click **Install with editing**. Once the model is ready, select **Qwen-Image 2.1** in **Generate**, add a file under **Reference images**, and describe the edit, such as `Change the car to dark green`. Click **Queue generation**.

## More help

- [Setup and maintenance](docs/OPERATIONS.md) covers CLI usage, LAN access, troubleshooting, updates, and backups.
- [Acceptance checklist](docs/ACCEPTANCE.md) covers automated tests and checks that require a GPU.

## License

Mirage and its pinned `stable-diffusion.cpp` dependency each use the MIT license. Model weights have separate licenses. [Mirage license](LICENSE) · [stable-diffusion.cpp license](https://github.com/leejet/stable-diffusion.cpp/blob/52491bd9f1fc15281791723bdff13baab7fd6819/LICENSE) · [Model registry](config/models.json)
