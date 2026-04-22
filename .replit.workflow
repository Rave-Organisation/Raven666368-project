# Mirrored from Replit. Sensitive [userenv.*] tables stripped.
# Source of truth lives in the Replit project.

modules = ["nodejs-24", "python-3.11"]

[[artifacts]]
id = "artifacts/api-server"

[[artifacts]]
id = "artifacts/mockup-sandbox"

[deployment]
router = "application"
deploymentTarget = "autoscale"

[deployment.postBuild]
args = ["pnpm", "store", "prune"]
env = { "CI" = "true" }

[workflows]
runButton = "Project"

[[workflows.workflow]]
name = "Project"
mode = "parallel"
author = "agent"

[[workflows.workflow.tasks]]
task = "workflow.run"
args = "engine: Paper Engine"

[[workflows.workflow]]
name = "engine: Paper Engine"
author = "agent"

[workflows.workflow.metadata]
outputType = "console"

[[workflows.workflow.tasks]]
task = "shell.exec"
args = ".pythonlibs/bin/uvicorn engine.paper_engine:app --host 0.0.0.0 --port 8000"
waitForPort = 8000

[agent]
stack = "PNPM_WORKSPACE"
expertMode = true
integrations = ["github:1.0.0"]

[postMerge]
path = "scripts/post-merge.sh"
timeoutMs = 20000

[nix]
channel = "stable-25_05"
packages = ["cargo", "libiconv", "libxcrypt", "openssl", "pkg-config", "rustc"]

[objectStorage]
defaultBucketID = "replit-objstore-6dcd2748-cd1c-4125-a27e-5d904c22f595"
