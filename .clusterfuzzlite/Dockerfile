# ClusterFuzzLite's Python builder: Atheris, PyInstaller and libFuzzer.
# Pinned by digest (OpenSSF Scorecard's Pinned-Dependencies); Dependabot's
# docker entry for this directory moves the digest.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:21f6fab17ace4dc76e1c56de91ee95d3d03da021df94b41056ba036f6c6f55e9

COPY . $SRC/igni-core
WORKDIR $SRC/igni-core
COPY .clusterfuzzlite/build.sh $SRC/
