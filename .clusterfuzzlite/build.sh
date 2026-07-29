#!/bin/bash -eu

project_root="$SRC/platform-gitops"
fuzzer_name="forge_plan_fuzzer"
fuzzer_source="$project_root/.clusterfuzzlite/fuzzers/${fuzzer_name}.py"
package_name="${fuzzer_name}.pkg"

pyinstaller \
  --clean \
  --distpath "$OUT" \
  --workpath "$WORK/pyinstaller" \
  --specpath "$WORK" \
  --onefile \
  --name "$package_name" \
  --paths "$project_root/scripts" \
  "$fuzzer_source"

cat >"$OUT/$fuzzer_name" <<'EOF'
#!/bin/sh
# LLVMFuzzerTestOneInput is required for ClusterFuzzLite target detection.
this_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$this_dir/forge_plan_fuzzer.pkg" "$@"
EOF
chmod +x "$OUT/$fuzzer_name"

python3 - "$project_root" "$OUT/${fuzzer_name}_seed_corpus.zip" <<'PY'
from pathlib import Path
import sys
import zipfile

root = Path(sys.argv[1])
destination = Path(sys.argv[2])
seeds = sorted((root / "examples" / "migrations").glob("*.json"))
if not seeds:
    raise SystemExit("no migration-plan seed files were found")

with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for seed in seeds:
        info = zipfile.ZipInfo(seed.name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, seed.read_bytes())
PY
