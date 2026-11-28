# ClusterFuzzLite's Python builder: Atheris, PyInstaller and libFuzzer.
# Pinned by digest (OpenSSF Scorecard's Pinned-Dependencies); Dependabot's
# docker entry for this directory moves the digest.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:ed78b82bdc5a9fde750bc83f50594042a8918f2ddb5a2f6e7b1a4b8ad79c893c

COPY . $SRC/igni-core
WORKDIR $SRC/igni-core
COPY .clusterfuzzlite/build.sh $SRC/
