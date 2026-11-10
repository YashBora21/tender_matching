# Public Docker Hub release commands

These are operator commands for the approved release candidate, not a record of
an image push. No image was built or published while preparing this guide.
Run PowerShell from the repository root with Docker Desktop in Linux-container
mode. Stop on any failed check. The release-candidate tag and confirmed evaluation
labels must exist before release evaluation; keep grouping disabled.

## 1. Freeze inputs and create public repositories

In Docker Hub, create `tender-backend` and `tender-frontend` under your namespace,
selecting **Public** explicitly. Do not rely on the account's default privacy.
Under each repository's **Settings > General > Tag mutability**, select immutable
tags for release tags (or all tags). Verify the settings before pushing.
[Repository creation](https://docs.docker.com/docker-hub/repos/create/),
[visibility](https://docs.docker.com/docker-hub/repos/manage/access/),
[immutable tags](https://docs.docker.com/docker-hub/repos/manage/hub-images/immutable-tags/).

```powershell
function Invoke-ReleaseDocker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed; stop this release.' }
}

$ReleaseNamespace = Read-Host 'Docker Hub namespace (user or organisation)'
$ReleaseGitTag = Read-Host 'Approved release-candidate Git tag'
$ReleaseSha = (git rev-parse "${ReleaseGitTag}^{commit}").Trim()
if ($LASTEXITCODE -ne 0) { throw 'RC Git tag does not resolve.' }
if ((git rev-parse HEAD).Trim() -ne $ReleaseSha) { throw 'Use a clean checkout of the RC.' }
if (git status --porcelain) { throw 'Commit or exclude outstanding work before building the RC.' }

$ReleaseImageTag = "rc-$($ReleaseSha.Substring(0,12))"
$ReleaseBackend = "${ReleaseNamespace}/tender-backend:${ReleaseImageTag}"
$ReleaseFrontend = "${ReleaseNamespace}/tender-frontend:${ReleaseImageTag}"
$ReleaseBrowserApi = Read-Host 'Public backend URL used by the browser'
$ReleaseApiUri = [Uri]$ReleaseBrowserApi
if (-not $ReleaseApiUri.IsAbsoluteUri -or $ReleaseApiUri.Scheme -notin @('http','https') -or
    $ReleaseApiUri.UserInfo -or $ReleaseApiUri.Query -or $ReleaseApiUri.Fragment) {
    throw 'Use an absolute HTTP(S) browser API URL without credentials, query or fragment.'
}
$ReleasePrivateConfig = (Resolve-Path (Read-Host 'Ignored private runtime dotenv file for leak checks')).Path
$ReleaseAudit = Join-Path $env:TEMP ("tender-image-audit-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $ReleaseAudit | Out-Null

Invoke-ReleaseDocker version
Invoke-ReleaseDocker buildx version
Invoke-ReleaseDocker buildx inspect --bootstrap
```

The frontend API address is deliberately public and baked into JavaScript. It is
the address the **browser** reaches, not a Docker service hostname. Changing
`VITE_API_URL` in `docker run` does not update the existing frontend bundle;
rebuild it for another backend address. Never put keys, provider model IDs or
provider URLs into build arguments. Docker build arguments/environment are not
secret storage. [Build secrets](https://docs.docker.com/build/building/secrets/).

## 2. Build both images locally, without pushing

```powershell
Invoke-ReleaseDocker buildx build --platform linux/amd64 --pull --load `
    --label "org.opencontainers.image.revision=$ReleaseSha" `
    --label "org.opencontainers.image.version=$ReleaseImageTag" `
    --metadata-file (Join-Path $ReleaseAudit 'backend-build.json') `
    --tag $ReleaseBackend ./backend

Invoke-ReleaseDocker buildx build --platform linux/amd64 --pull --load `
    --label "org.opencontainers.image.revision=$ReleaseSha" `
    --label "org.opencontainers.image.version=$ReleaseImageTag" `
    --build-arg "VITE_API_URL=$ReleaseBrowserApi" `
    --metadata-file (Join-Path $ReleaseAudit 'frontend-build.json') `
    --tag $ReleaseFrontend ./frontend

foreach ($ReleaseImage in @($ReleaseBackend,$ReleaseFrontend)) {
    $ReleaseMetadata = (docker image inspect $ReleaseImage | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0) { throw 'Image inspection failed.' }
    if ($ReleaseMetadata.Os -ne 'linux' -or $ReleaseMetadata.Architecture -ne 'amd64') {
        throw 'Unexpected image platform.'
    }
    if ($ReleaseMetadata.Config.Labels.'org.opencontainers.image.revision' -ne $ReleaseSha) {
        throw 'Image revision does not match the approved RC.'
    }
    $ReleaseBakedPrivate = @($ReleaseMetadata.Config.Env | Where-Object {
        ($_ -split '=',2)[0] -match '(^|_)(API_KEY|MODEL|BASE_URL)$'
    })
    if ($ReleaseBakedPrivate.Count) { throw 'Private runtime configuration is baked into image ENV.' }
}
```

`--load` loads a single-platform result for inspection before publishing.
This command set publishes `linux/amd64`; it does not claim ARM64 or CUDA/Paddle
compatibility. [Buildx build options](https://docs.docker.com/reference/cli/docker/buildx/build/).

Base image tags and dependency ranges are currently not digest/version locked.
Record the resulting registry digests; the Git hash alone cannot reproduce every
future rebuild byte-for-byte.

## 3. Check the actual image files without starting the application

```powershell
$ReleaseSuffix = [Guid]::NewGuid().ToString('N').Substring(0,8)
$ReleaseBackendAudit = "tender-backend-audit-$ReleaseSuffix"
$ReleaseFrontendAudit = "tender-frontend-audit-$ReleaseSuffix"
try {
    Invoke-ReleaseDocker create --name $ReleaseBackendAudit --entrypoint /bin/true $ReleaseBackend
    Invoke-ReleaseDocker create --name $ReleaseFrontendAudit --entrypoint /bin/true $ReleaseFrontend
    Invoke-ReleaseDocker cp "${ReleaseBackendAudit}:/app/app" (Join-Path $ReleaseAudit 'backend-app')
    Invoke-ReleaseDocker cp "${ReleaseBackendAudit}:/app/data" (Join-Path $ReleaseAudit 'backend-config')
    Invoke-ReleaseDocker cp "${ReleaseFrontendAudit}:/usr/share/nginx/html" (Join-Path $ReleaseAudit 'frontend-html')

    & .\venv\Scripts\python.exe scripts/check_public_image.py `
        --runtime-env $ReleasePrivateConfig `
        --root (Join-Path $ReleaseAudit 'backend-app') `
        --root (Join-Path $ReleaseAudit 'backend-config') `
        --root (Join-Path $ReleaseAudit 'frontend-html') `
        --config-root (Join-Path $ReleaseAudit 'backend-config')
    if ($LASTEXITCODE -ne 0) { throw 'Exported image contents failed the publication gate.' }
} finally {
    docker container rm $ReleaseBackendAudit $ReleaseFrontendAudit | Out-Null
}
```

The gate compares exact configured API-key/model/provider values, including
JSON/slash-escaped forms, without printing them. It rejects `.env`, test/corpus
artifacts and unexpected configuration files. Historical source-only documents
are outside this scan: the command checks exported **built application files**,
not every historical Markdown note. It is not a proof against arbitrary encoding
or unknown secrets. Do not print full runtime container inspection, private env
files or unredacted application logs into release evidence.

The backend packages exactly these nonsecret JSON files under `/app/data`:
`attribute_vocabulary.json`, `categories.json`, `doc_types.json`, `doc_roles.json`,
`normalization_policy.json`. The writable runtime volume is `/var/lib/tender/data`;
mounting there does not hide packaged policy. The corpus, uploads, page caches,
tests, credentials and embedding weights are not shipped in the image.

## 4. Runtime smoke checks on disposable data

Prepare **ignored**, UTF-8-without-BOM Docker env files privately. Do not copy the
host `.env` blindly: local host paths and `localhost` database addresses can be
wrong inside containers. The repository's current private file has previously
failed Docker's BOM parsing; do not edit or dump it merely to validate Compose.

- Database env file: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` for a new
  disposable database.
- Backend env file: matching `DATABASE_URL` with hostname `tender-release-db`,
  `DATA_DIR=/var/lib/tender/data`, browser-appropriate `CORS_ORIGINS`, and the
  validated runtime embedding configuration. Provider settings are runtime-only.
- The app warms OCR and embeddings before serving. Provide the validated local
  weights/cache at runtime; the image does not contain them. Use offline flags
  with an existing cache when the smoke check must make no network inference.
- The current Dockerfile installs Tesseract. Windows Paddle worker paths and
  locally installed CUDA dependencies are not portable into this Linux image.
  A Paddle/CUDA release profile requires its own built-image validation; a
  Tesseract health check does not establish Paddle/OCR quality or feature parity.

These commands create new named resources and run startup migrations against
that **new database only**. Do not point the runtime env file at the development
or historical evaluation database.

```powershell
$ReleasePgEnv = (Resolve-Path (Read-Host 'Ignored disposable PostgreSQL env file')).Path
$ReleaseRuntimeEnv = (Resolve-Path (Read-Host 'Ignored container-ready backend env file')).Path
$ReleaseHfCache = (Resolve-Path (Read-Host 'Validated runtime Hugging Face cache directory')).Path
if (@(docker ps -a --format '{{.Names}}') | Where-Object { $_ -in @('tender-release-db','tender-release-backend','tender-release-frontend') }) {
    throw 'Release container names already exist; inspect them before proceeding.'
}
Invoke-ReleaseDocker network create tender-release-net
Invoke-ReleaseDocker volume create tender-release-pgdata
Invoke-ReleaseDocker volume create tender-release-data
Invoke-ReleaseDocker run -d --name tender-release-db --network tender-release-net `
    --env-file $ReleasePgEnv --mount source=tender-release-pgdata,target=/var/lib/postgresql/data `
    --health-cmd 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' `
    --health-interval 5s --health-retries 20 pgvector/pgvector:pg16

Invoke-ReleaseDocker run --rm --user 0 --network none --entrypoint sh `
    --mount source=tender-release-data,target=/var/lib/tender/data $ReleaseBackend `
    -c 'chown -R 10001:10001 /var/lib/tender/data'
```

Wait for the database before starting the backend, which runs migrations immediately.
These checks emit health status only, not configuration values:

```powershell
function Wait-ReleaseHealthy([string]$Name) {
    for ($ReleaseAttempt=0; $ReleaseAttempt -lt 150; $ReleaseAttempt++) {
        $ReleaseState = (docker inspect --format '{{.State.Status}}' $Name).Trim()
        $ReleaseHealth = (docker inspect --format '{{.State.Health.Status}}' $Name).Trim()
        if ($ReleaseState -eq 'exited') { throw 'Release container exited; inspect logs privately and redact them.' }
        if ($ReleaseHealth -eq 'healthy') { return }
        Start-Sleep -Seconds 2
    }
    throw 'Release health deadline exceeded.'
}
Wait-ReleaseHealthy tender-release-db
Invoke-ReleaseDocker run -d --name tender-release-backend --network tender-release-net `
    --env-file $ReleaseRuntimeEnv --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 `
    --env HF_HOME=/var/cache/huggingface `
    --mount source=tender-release-data,target=/var/lib/tender/data `
    --mount "type=bind,source=$ReleaseHfCache,target=/var/cache/huggingface" `
    -p 8000:8000 $ReleaseBackend
Wait-ReleaseHealthy tender-release-backend
Invoke-ReleaseDocker run -d --name tender-release-frontend --network tender-release-net `
    -p 3000:80 $ReleaseFrontend
Wait-ReleaseHealthy tender-release-frontend
(Invoke-WebRequest 'http://localhost:8000/api/health').StatusCode
(Invoke-WebRequest 'http://localhost:3000/healthz').StatusCode
(Invoke-WebRequest 'http://localhost:3000/').StatusCode
```

Expect HTTP 200 for all three. Then verify the browser's requests reach the
configured public API URL. An empty fresh database is not a populated matching
system: the frozen corpus must be mounted/imported and indexed through the
approved release procedure before matching evaluation. Startup migrations alone
do not ingest or embed a corpus. Do not use old dev DB results to claim this
image's evaluation passed.

## 5. Authenticate and push only after the preceding gates pass

```powershell
$ReleaseHubUser = Read-Host 'Docker Hub login username (not an organisation namespace)'
Invoke-ReleaseDocker login --username $ReleaseHubUser
# Paste a Docker Hub personal access token only at Docker's interactive prompt.
Invoke-ReleaseDocker push $ReleaseBackend
Invoke-ReleaseDocker push $ReleaseFrontend
```

The token is never a command argument, build argument, repository file or image
layer. `docker login` without a username also supports Docker's browser/device
flow. [Authentication](https://docs.docker.com/reference/cli/docker/login/).

Do not retag `latest` before acceptance. Immutable tags reject overwrites; use a
new RC tag for a changed candidate. Record both pushed manifest digests, not only
the convenient tags.

## 6. Prove both repositories are public using a credential-free pull

```powershell
$ReleaseEngine = (docker context inspect --format '{{.Endpoints.docker.Host}}').Trim()
$ReleaseAnonConfig = Join-Path $ReleaseAudit 'anonymous-docker-config'
New-Item -ItemType Directory -Path $ReleaseAnonConfig | Out-Null
Invoke-ReleaseDocker --config $ReleaseAnonConfig --host $ReleaseEngine image pull --platform linux/amd64 $ReleaseBackend
Invoke-ReleaseDocker --config $ReleaseAnonConfig --host $ReleaseEngine image pull --platform linux/amd64 $ReleaseFrontend

foreach ($ReleaseRepo in @('tender-backend','tender-frontend')) {
    $ReleasePublic = Invoke-RestMethod "https://hub.docker.com/v2/namespaces/$ReleaseNamespace/repositories/$ReleaseRepo"
    if ($ReleasePublic.is_private) { throw 'Repository is private.' }
}
Invoke-ReleaseDocker image inspect $ReleaseBackend --format '{{json .RepoDigests}}'
Invoke-ReleaseDocker image inspect $ReleaseFrontend --format '{{json .RepoDigests}}'
```

These anonymous CLI commands assume a local Docker Desktop engine; remote TLS
contexts may need a separate clean client with the correct transport settings.
Also open both repository pages in a private browser session. Public pulls must
work without Hub login; authentication/rate-limit/network failures are not a
successful public-visibility check. Deploy by the recorded `repository@sha256:...`
digest when exact content identity matters.
[Anonymous repository access](https://docs.docker.com/reference/api/hub/latest/operations/GetRepository/),
[pulling by digest](https://docs.docker.com/reference/cli/docker/image/pull/).

Record the RC commit, both image references/digests, platform, build metadata,
public visibility results and smoke status in `eval/audit`. Do not put private
runtime values in that record. Keep the RC tag fixed even when a later audit-only
commit records publication evidence.
