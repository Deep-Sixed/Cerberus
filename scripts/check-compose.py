"""Validate the bundle with synthetic credentials; never access operator secrets."""
import os
import subprocess

env = os.environ.copy()
for key in ("CERBERUS_API_TOKEN", "CERBERUS_ADMIN_TOKEN", "CB_KEY_DEV", "OPENROUTER_API_KEY"):
    env[key] = "cb-validation-placeholder"
subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-f",
                "deploy/fusion/compose.yaml", "config", "--quiet"], env=env, check=True)
