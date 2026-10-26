# Container dependency and base-image updates

Each service's `requirements.txt` defines its accepted dependency ranges;
`requirements.lock` pins the complete Python 3.12 Linux/amd64 dependency set
and hashes. Docker installs only the lock file with pip's hash verification.
When a dependency declaration changes, regenerate the corresponding lock file
from the repository root with:

```bash
uv pip compile --no-header --python-version 3.12 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --output-file services/agent_gateway_v3/requirements.lock \
  services/agent_gateway_v3/requirements.txt
```

Repeat for `agent_persistence_worker_v3`, then run the service tests and build
both images. The Dockerfiles also pin the Python 3.12 slim amd64 base image by
registry digest and run the app as UID/GID `10001`; review and update that
digest as a separate, tested maintenance change. Keep the two middleware
templates' service lock files and Dockerfiles in sync.
