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
command -v flock >/dev/null || { echo "flock is required" >&2; exit 127; }
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
  infrastructure/import/gmail-bootstrap-job.yaml
  infrastructure/import/gmail-import-cronjobs.yaml
  infrastructure/import/gmail-recurring-cronjobs.yaml
  infrastructure/import/gmail-full-pilot-admission.yaml
  infrastructure/migration/migration-job.yaml
)
transaction_dir="$(git rev-parse --git-path coop-image-promotion-transaction)"
[[ "$transaction_dir" == /* ]] || transaction_dir="${ROOT}/${transaction_dir}"
initializing_dir="$(git rev-parse --git-path coop-image-promotion-transaction.initializing)"
[[ "$initializing_dir" == /* ]] || initializing_dir="${ROOT}/${initializing_dir}"
lock_file="$(git rev-parse --git-path coop-image-promotion.lock)"
[[ "$lock_file" == /* ]] || lock_file="${ROOT}/${lock_file}"
exec 9>"$lock_file"
flock -n 9 || { echo "Another image promotion is already running" >&2; exit 4; }
restore_transaction() {
  local target
  for target in "${promotion_targets[@]}"; do
    [[ -f "${transaction_dir}/backup/${target}" ]] || {
      echo "Incomplete promotion recovery data for ${target}; refusing to continue" >&2
      return 1
    }
  done
  for target in "${promotion_targets[@]}"; do
    cp -- "${transaction_dir}/backup/${target}" "$target"
  done
  rm -rf -- "$transaction_dir"
}
if [[ -d "$initializing_dir" ]]; then
  echo "Discarding incomplete image-promotion initialization" >&2
  rm -rf -- "$initializing_dir"
fi
if [[ -d "$transaction_dir" ]]; then
  echo "Recovering interrupted image-promotion transaction" >&2
  restore_transaction
fi
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

mkdir -p "${initializing_dir}/backup" "${initializing_dir}/staged"
for target in "${promotion_targets[@]}"; do
  mkdir -p "${initializing_dir}/backup/$(dirname "$target")"
  cp -- "$target" "${initializing_dir}/backup/${target}"
done
cp -a -- infrastructure "${initializing_dir}/staged/"
mv -- "$initializing_dir" "$transaction_dir"
rollback_manifests() {
  local status=$?
  trap - ERR INT TERM
  restore_transaction
  exit "$status"
}
trap rollback_manifests ERR INT TERM

python3 - "$tag" "$api_digest" "$web_digest" "$short_sha" "$git_sha" "${transaction_dir}/staged" <<'PY'
from pathlib import Path
import re
import sys

tag, api_digest, web_digest, short_sha, git_sha, staging_arg = sys.argv[1:]
staging = Path(staging_arg)


def staged(path: str) -> Path:
    return staging / path

def update_image(path: str, image: str, digest: str) -> None:
    file = staged(path)
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

for path, expected_count in (
    ("infrastructure/import/gmail-bootstrap-job.yaml", 1),
    ("infrastructure/import/gmail-import-cronjobs.yaml", 1),
    ("infrastructure/import/gmail-recurring-cronjobs.yaml", 2),
):
    cronjob = staged(path)
    text = cronjob.read_text()
    updated, count = re.subn(
        r'(?m)^(    coop\.example\.com/image-source-revision: ")[0-9a-f]{40}("$)',
        rf"\g<1>{git_sha}\g<2>",
        text,
    )
    if count != expected_count:
        raise SystemExit(
            f"expected {expected_count} Gmail image source revisions in {path}, got {count}"
        )
    cronjob.write_text(updated)

admission = staged("infrastructure/import/gmail-full-pilot-admission.yaml")
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

bootstrap = staged("infrastructure/import/gmail-bootstrap-job.yaml")
text = bootstrap.read_text()
updated, count = re.subn(
    r"(?m)^  name: gmail-import-bootstrap-[0-9a-f]{12}$",
    f"  name: gmail-import-bootstrap-{short_sha}",
    text,
)
if count != 1:
    raise SystemExit(f"expected exactly one operational bootstrap release name, got {count}")
bootstrap.write_text(updated)

job = staged("infrastructure/migration/migration-job.yaml")
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
  staged_dir="${transaction_dir}/staged/${dir}"
  if command -v kustomize >/dev/null; then
    kustomize build "$staged_dir" >/dev/null
  elif command -v kubectl >/dev/null; then
    kubectl kustomize "$staged_dir" >/dev/null
  fi
done

for target in "${promotion_targets[@]}"; do
  temp_target="$(mktemp "$(dirname "$target")/.promotion.XXXXXX")"
  cp -- "${transaction_dir}/staged/${target}" "$temp_target"
  chmod --reference="$target" "$temp_target"
  mv -f -- "$temp_target" "$target"
done

trap - ERR INT TERM
rm -rf -- "$transaction_dir"

printf 'Promoted manifests only (no cluster mutation):\n  API %s:%s@%s\n  Web %s:%s@%s\n' \
  "$api_image" "$tag" "$api_digest" "$web_image" "$tag" "$web_digest"
