#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

REGISTRY="${REGISTRY:-127.0.0.1:5000}"
[[ "$REGISTRY" == "127.0.0.1:5000" ]] || {
  echo "REGISTRY must be 127.0.0.1:5000 for this local promotion workflow" >&2
  exit 2
}
command -v docker >/dev/null || { echo "docker is required" >&2; exit 127; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 127; }
(command -v kustomize >/dev/null || command -v kubectl >/dev/null) || { echo "kustomize or kubectl is required" >&2; exit 127; }

git_sha="$(git rev-parse HEAD)"
short_sha="$(git rev-parse --short=12 HEAD)"
tag="git-${short_sha}"
api_image="${REGISTRY}/coop-api"
web_image="${REGISTRY}/coop-web"

promotion_targets=(
  infrastructure/apps/kustomization.yaml
  infrastructure/migration/kustomization.yaml
  infrastructure/import/kustomization.yaml
  infrastructure/import/gmail-import-cronjobs.yaml
  infrastructure/import/gmail-full-pilot-admission.yaml
  infrastructure/migration/migration-job.yaml
)
if [[ -n "$(git status --porcelain --untracked-files=all -- apps packages package.json pnpm-lock.yaml pnpm-workspace.yaml "${promotion_targets[@]}")" ]]; then
  echo "Refusing to build: application inputs or promotion targets differ from commit ${git_sha}." >&2
  exit 3
fi

docker build --file apps/api/Dockerfile \
  --label "org.opencontainers.image.revision=${git_sha}" --tag "${api_image}:${tag}" apps/api
docker build --file apps/web/Dockerfile \
  --build-arg NEXT_PUBLIC_API_URL=/api/v1 \
  --label "org.opencontainers.image.revision=${git_sha}" --tag "${web_image}:${tag}" .
docker push "${api_image}:${tag}"
docker push "${web_image}:${tag}"

resolve_digest() {
  local ref="$1" digest
  digest="$(docker buildx imagetools inspect "$ref" --format '{{json .Manifest.Digest}}' | tr -d '"')"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "Could not verify registry digest for ${ref}: ${digest}" >&2
    return 1
  }
  printf '%s\n' "$digest"
}

api_digest="$(resolve_digest "${api_image}:${tag}")"
web_digest="$(resolve_digest "${web_image}:${tag}")"

backup_dir="$(mktemp -d)"
for target in "${promotion_targets[@]}"; do
  mkdir -p "${backup_dir}/$(dirname "$target")"
  cp -- "$target" "${backup_dir}/${target}"
done
rollback_manifests() {
  local status=$?
  trap - ERR INT TERM
  for target in "${promotion_targets[@]}"; do
    cp -- "${backup_dir}/${target}" "$target"
  done
  rm -rf -- "$backup_dir"
  exit "$status"
}
trap rollback_manifests ERR INT TERM

python3 - "$tag" "$api_digest" "$web_digest" "$short_sha" "$git_sha" <<'PY'
from pathlib import Path
import re
import sys

tag, api_digest, web_digest, short_sha, git_sha = sys.argv[1:]

def update_image(path: str, image: str, digest: str) -> None:
    file = Path(path)
    text = file.read_text()
    pattern = re.compile(
        rf"(?m)(  - name: {re.escape(image)}\n    newTag: )[^\n]+(\n    digest: )[^\n]+"
    )
    updated, count = pattern.subn(rf"\g<1>{tag}\g<2>{digest}", text)
    if count != 1:
        raise SystemExit(f"expected exactly one image entry for {image} in {path}, got {count}")
    file.write_text(updated)

update_image("infrastructure/apps/kustomization.yaml", "127.0.0.1:5000/coop-api", api_digest)
update_image("infrastructure/apps/kustomization.yaml", "127.0.0.1:5000/coop-web", web_digest)
update_image("infrastructure/migration/kustomization.yaml", "127.0.0.1:5000/coop-api", api_digest)
update_image("infrastructure/import/kustomization.yaml", "127.0.0.1:5000/coop-api", api_digest)

cronjob = Path("infrastructure/import/gmail-import-cronjobs.yaml")
text = cronjob.read_text()
updated, count = re.subn(
    r'(?m)^(    coop\.example\.com/image-source-revision: ")[0-9a-f]{40}("$)',
    rf"\g<1>{git_sha}\g<2>",
    text,
)
if count != 1:
    raise SystemExit(f"expected exactly one Gmail image source revision, got {count}")
cronjob.write_text(updated)

admission = Path("infrastructure/import/gmail-full-pilot-admission.yaml")
text = admission.read_text()
pilot_image = f"127.0.0.1:5000/coop-api:{tag}@{api_digest}"
updated, image_count = re.subn(
    r'(?m)^(  image: ")[^"]+("$)',
    rf"\g<1>{pilot_image}\g<2>",
    text,
)
updated, revision_count = re.subn(
    r'(?m)^(  imageSourceRevision: ")[0-9a-f]{40}("$)',
    rf"\g<1>{git_sha}\g<2>",
    updated,
)
if image_count != 1 or revision_count != 1:
    raise SystemExit(
        "expected exactly one admission image and source revision, "
        f"got image={image_count}, revision={revision_count}"
    )
admission.write_text(updated)

job = Path("infrastructure/migration/migration-job.yaml")
text = job.read_text()
updated, count = re.subn(
    r"(?m)^  name: coop-migration-[0-9a-f]{12}$",
    f"  name: coop-migration-{short_sha}",
    text,
)
if count != 1:
    raise SystemExit(f"expected exactly one migration release name, got {count}")
job.write_text(updated)
PY

for dir in infrastructure/apps infrastructure/migration infrastructure/import; do
  if command -v kustomize >/dev/null; then
    kustomize build "$dir" >/dev/null
  elif command -v kubectl >/dev/null; then
    kubectl kustomize "$dir" >/dev/null
  fi
done

trap - ERR INT TERM
rm -rf -- "$backup_dir"

printf 'Promoted manifests only (no cluster mutation):\n  API %s:%s@%s\n  Web %s:%s@%s\n' \
  "$api_image" "$tag" "$api_digest" "$web_image" "$tag" "$web_digest"
