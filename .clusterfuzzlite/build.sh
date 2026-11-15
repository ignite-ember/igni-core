#!/bin/bash -eu
# Builds each fuzz/fuzz_*.py into a libFuzzer target in $OUT.
#
# The builder's interpreter is Python 3.11 and igni-core declares 3.12
# exactly, so the package is not installed: its source is put on
# PyInstaller's path instead, and only the dependencies the fuzzed modules
# import are installed — from requirements.txt beside this script, every
# one pinned by hash, at the versions uv.lock carries. To regenerate it
# after a lock change:
#
#   uv export --frozen --all-extras --no-hashes --no-emit-project \
#     --no-header --no-annotate | sed 's/ *;.*//' > /tmp/constraints.txt
#   uv pip compile .clusterfuzzlite/requirements.in -c /tmp/constraints.txt \
#     --python-version 3.11 --python-platform x86_64-manylinux_2_31 \
#     --generate-hashes -o .clusterfuzzlite/requirements.txt

pip3 install --require-hashes --no-deps -r "$SRC/igni-core/.clusterfuzzlite/requirements.txt"

for fuzzer in "$SRC"/igni-core/fuzz/fuzz_*.py; do
  name=$(basename -s .py "$fuzzer")
  compile_python_fuzzer "$fuzzer" --paths "$SRC/igni-core/src"
  seeds="$SRC/igni-core/fuzz/corpus/$name"
  if [[ -d "$seeds" ]]; then
    (cd "$seeds" && zip -q -r "$OUT/${name}_seed_corpus.zip" .)
  fi
done
