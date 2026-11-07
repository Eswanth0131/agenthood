# Tinfoil-hosted singing for Agenthood

ACE-Step runs on one NVIDIA GPU in a Tinfoil Container. The studio sends the approved
lyrics, brief, feedback, and backing sketch only after the project owner checks the
consent box for that take. The studio verifies the enclave against the public
configuration repository with the Tinfoil SDK and pins the verified TLS connection before it sends the request. The
authenticated gateway exposes only rendering, job status, cancellation, and health.
The studio saves the resulting sung performance and uses it for the final mix.
The model may reinterpret the backing sketch.

## Check access first

In the [Tinfoil dashboard](https://dash.tinfoil.sh), open **Containers → Instances → New
instance** and see whether your hackathon organization offers a GPU instance.
A standard inference API key alone does not grant GPU Container access.
Review the allocation and billing shown there before creating an instance.
See [Tinfoil’s quickstart](https://docs.tinfoil.sh/containers/quickstart).

## Prepare the measured release

1. Create a public configuration repository from the
   [official template](https://github.com/tinfoilsh/tinfoil-containers-template).
   Keep both Tinfoil release workflows. Copy this deployment directory to
   `deploy/tinfoil` in that repository. It contains only the renderer code,
   build recipe, and configuration examples.
2. Copy `deploy/tinfoil/build-image.yml.example` to
   `.github/workflows/build-ace-step.yml`. Commit, then run **Build ACE-Step GPU image**
   from GitHub Actions. The build downloads and checksum-verifies the three public
   model files on the runner. It builds the pinned ACE-Step source for Linux x86-64
   and NVIDIA Hopper and Blackwell GPUs. The workflow summary gives the immutable
   `ghcr.io/…@sha256:…` image reference. Make that package accessible to Tinfoil
   according to its [registry documentation](https://docs.tinfoil.sh/containers/private-images).
3. Copy `tinfoil-config.yml.example` from this directory over the template's
   `tinfoil-config.yml` at the root of the config repo. Set the image digest and use
   a CVM version supported by the Tinfoil dashboard. Commit the configuration and
   run the template's **Tinfoil Release** workflow. Wait for both workflows and
   confirm the measured GitHub release is published.
4. Generate a token with `openssl rand -hex 32`. Enter the value in your Tinfoil
   project's **Secrets** as `SONG_RENDER_TOKEN`. Keep the value out of Git, the
   image, and chat. Create a GPU instance from the measured release and wait for
   **Running**. Ensure the release is the config repository's latest published
   release, which is what the Python SDK verifies.

## Connect the studio

Install the optional SDK in the studio's environment:

```bash
python -m pip install -r requirements-tinfoil.txt
```

Set these values in the studio's local `.env`, then restart the studio:

```dotenv
AGENTHOOD_SONG_URL=https://your-instance.your-org.containers.tinfoil.dev
AGENTHOOD_SONG_TINFOIL_REPO=your-org/your-config-repo
AGENTHOOD_SONG_TOKEN=your-SONG_RENDER_TOKEN-value
```

For a direct readiness check, use the official attested CLI and the same config
repository. The health endpoint exposes no song or job data:

```bash
tinfoil http get "https://your-instance.your-org.containers.tinfoil.dev/health" \
  -e your-instance.your-org.containers.tinfoil.dev -r your-org/your-config-repo
```

Expect `{"status":"ok"}`. For an existing song, select it, check the consent box
and click **Generate sung take and update mix**. Play **Sung take · ACE-Step** and
**Final mix**. An unsuccessful hosted request preserves the earlier take. For a new
song, the consent box appears before the singer stage.

To switch back to local ACE-Step, point `AGENTHOOD_SONG_URL` at
`http://127.0.0.1:8085` and restart. The local connection does not need a token
or Tinfoil SDK.
